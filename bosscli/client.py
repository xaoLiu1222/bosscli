"""
BossClient — off-device BOSS直聘 API client (search + authenticated read endpoints).

A session (t2 auth token + this device's client_info/uniqid + cardlist template) is the
only account-bound input; extract it once from a logged-in device, or obtain t2 with the
login flow (bosscli.login). See session.example.json.

Signing has two branches, both reproduced in signer.py:
  * batch  (/api/batch/requests, subReqs) — used by search().
  * plain  (ordinary GET/POST)            — used by the login endpoints and the read endpoints.
Search + login sit on the app's no-secretKey whitelist, so their sign key is None; call_plain /
call_batch expose a `key` argument so endpoints that DO need the account secretKey can pass it.
"""
from __future__ import annotations
import json, os, time, uuid
import requests
from . import yzwg, signer

HOST = "https://api5.zhipin.com"
BATCH_PATH = "/api/batch/requests"
CARDLIST_PATH = "/api/zpgeek/app/geek/search/cardlist"

# Captured chat-image route, App v14.180 (2026-11-11). Both endpoints use account secretKey.
UPLOAD_HOST = "https://api-and.zhipin.com"
UPLOAD_PRECHECK_PATH = "/api/zpupload/quicklyUpload"
UPLOAD_IMAGE_PATH = "/api/zpupload/uploadSingle"


class AuthExpired(RuntimeError):
    """The server rejected t2 (code==7, 当前登录状态已失效). Re-login to refresh it."""
    def __init__(self, message: str = "登录状态已失效 (t2 过期)", resp: dict | None = None):
        super().__init__(message)
        self.resp = resp


class BossClient:
    def __init__(self, session: dict):
        self.s = session
        self.http = requests.Session()

    @classmethod
    def from_file(cls, path: str) -> "BossClient":
        with open(path, encoding='utf-8') as f:
            return cls(json.load(f))

    # ---- response decode: server picks encoding via zp-* headers ----
    @staticmethod
    def _decode(raw: bytes, key: str | None):
        # 0) plaintext JSON — the server returns some errors uncompressed/unencrypted, e.g.
        #    {"code":7,"message":"当前登录状态已失效"} (t2 expired) on the batch endpoint. Try
        #    this first so an auth error surfaces its message instead of crashing the RC4 path.
        if raw.lstrip()[:1] in (b'{', b'['):
            try:
                return json.loads(raw)
            except Exception:
                pass
        # A) base64url(RC4(json|frame))  B) raw RC4(frame(LZ4(json)))  C) raw RC4(json)
        try:
            dec = yzwg.rc4(yzwg._rc4_key(key), yzwg._unb64(raw.decode('ascii')))
            if dec[:8] == yzwg.MAGIC:
                dec = yzwg._deframe(dec)
            return json.loads(dec)
        except Exception:
            pass
        dec = yzwg.rc4(yzwg._rc4_key(key), raw)
        if dec[:8] == yzwg.MAGIC:
            return json.loads(yzwg._deframe(dec))
        return json.loads(dec)

    def _decode_resp(self, content: bytes, key: str | None):
        """Decode a response body. Non-whitelisted endpoints sign with the account secretKey; the
        server usually encrypts the reply with that same key, but occasionally replies null-keyed
        (or plaintext) — so try the request key first and fall back to None."""
        try:
            return self._decode(content, key)
        except Exception:
            if key is not None:
                return self._decode(content, None)
            raise

    @staticmethod
    def raise_for_auth(resp: dict) -> dict:
        """Turn a top-level code==7 into AuthExpired; pass anything else through."""
        if isinstance(resp, dict) and resp.get("code") == 7:
            raise AuthExpired(resp.get("message") or "登录状态已失效", resp)
        return resp

    def _headers(self) -> dict:
        return {
            "User-Agent": self.s.get("user_agent", "NetType/wifi Screen/1080X2209 BossZhipin/14.050 Android 36"),
            "zp-accept-encoding": "1", "zp-accept-compressing": "3", "zp-accept-encrypting": "1",
            "t2": self.s["t2"], "traceId": "A-" + str(uuid.uuid4()),
        }

    def _outer_params(self) -> dict:
        ci = self.s["client_info"]
        ci = ci if isinstance(ci, str) else json.dumps(ci, separators=(',', ':'), ensure_ascii=False)
        return {
            "client_info": ci,
            "curidentity": self.s.get("curidentity", "0"),
            "req_time": signer.now_ms(),
            "uniqid": self.s["uniqid"],
            "v": self.s.get("v", "14.050"),
        }

    def _wire(self, s: dict) -> str:
        """strD + signed sp/sig + app_id (appended AFTER signing), the on-the-wire param string."""
        return (s["strD"] + "&sp=" + requests.utils.quote(s["sp"], safe='')
                + "&sig=" + s["sig"] + "&app_id=" + self.s.get("app_id", "1003"))

    def _http(self, method: str, url: str, *, data=None, files=None, headers=None, retries: int = 2,
              allow_redirects: bool = True):
        """One HTTP call with a small retry/backoff on transport errors (not on 4xx/5xx bodies)."""
        last = None
        for attempt in range(retries + 1):
            try:
                r = self.http.request(method, url, data=data, files=files, headers=headers, timeout=20,
                                      allow_redirects=allow_redirects)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                if attempt < retries:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise
        raise last  # pragma: no cover

    # ---- generic signed calls (reused by search + the read endpoints) ----
    def call_plain(self, method: str, path: str, req_params: dict, key: str | None = None, *,
                   safe_write: bool = False) -> dict:
        """Sign an ordinary GET/POST (net.bosszhipin.base.m.e branch) and return the decoded body."""
        params = {**self._outer_params(), **{k: v for k, v in req_params.items() if v not in (None, "")}}
        s = signer.sign_request(params, path, key)
        wire = self._wire(s)
        options = {"retries": 0, "allow_redirects": False} if safe_write else {}
        if method.upper() == "GET":
            r = self._http("GET", HOST + path + "?" + wire, headers=self._headers(), **options)
        else:
            h = self._headers(); h["Content-Type"] = "application/x-www-form-urlencoded"
            r = self._http("POST", HOST + path, data=wire, headers=h, **options)
        if safe_write and 300 <= r.status_code < 400:
            raise ValueError("写接口返回重定向，已停止，不自动重试")
        return self.raise_for_auth(self._decode_resp(r.content, key))

    def call_batch(self, subreqs: list[dict], key: str | None = None) -> dict:
        """Sign a /api/batch/requests call (net.bosszhipin.base.m.f branch); subreqs is a list of
        {method, path, query} (and optional body). Returns the decoded envelope."""
        body_obj = {"subReqs": subreqs}
        body_json = json.dumps(body_obj, separators=(',', ':'), ensure_ascii=False)
        outer = self._outer_params()
        s = signer.sign_batch(outer, body_json, BATCH_PATH, key=key)
        q = self._wire(s)
        # App 14.180 emits POST with its encrypted batch body (not a GET carrying a body).
        r = self._http("POST", HOST + BATCH_PATH + "?" + q, data=s["encBody"], headers=self._headers())
        return self.raise_for_auth(self._decode_resp(r.content, key))

    # ---- search ----
    def search(self, query: str, city: str | None = None, page: int = 1, sort: int = -1,
               filter_params: dict | None = None) -> dict:
        """One page of geek job search. `filter_params` merges into the cardlist filterParams
        (e.g. {"salary": "407", "experience": "104,105"}); server honours these keys."""
        # Wire defaults observed in 14.180 keyword-search subrequests. Account-scoped
        # expectId/encryptExpectId must come from the session, never another user's fixture.
        cq = {"searchType": "3", "source": "1", "prefix": "q", "isFromJd": "0",
              "isSupplySearch": "false", "maskComType": "0", "noCorrect": "0",
              "queryGuideSearch": "0", **self.s.get("cardlist_defaults", {})}
        cq["query"] = query
        cq["page"] = str(page)
        cq["sort"] = str(sort)
        fp = json.loads(cq.get("filterParams", '{}'))
        if city:
            fp["cityCode"] = str(city)
        if filter_params:
            for k, v in filter_params.items():
                fp[k] = v
        cq["filterParams"] = json.dumps(fp, separators=(',', ':'), ensure_ascii=False)
        subreq_query = signer.build_query(cq)
        return self.call_batch([{"method": "GET", "path": CARDLIST_PATH, "query": subreq_query}], key=None)

    # ---- authenticated geek read endpoints (RE'd from v14.050) ----
    # Signing key: whitelisted paths (config.m.f43051a) use key=None like search; the rest use the
    # account secretKey (LBase.getSecretKey, stored in the session as `secretKey`).
    def _secret(self) -> str | None:
        return self.s.get("secretKey")

    def whoami(self, user_id: str = "0") -> dict:
        """GetUserAccountGeekDetailRequest — GET /api/zpgeek/cvapp/geek/baseinfo/query (key=secretKey).
        userId=0 means "me". Response geekDetail carries expectPositionList[].{expectId,encryptExpectId}."""
        return self.call_plain("GET", "/api/zpgeek/cvapp/geek/baseinfo/query",
                               {"userId": user_id}, key=self._secret())

    def job_detail(self, security_id: str, lid: str | None = None, expect_id: str | None = None,
                   query: str | None = None, extra: dict | None = None) -> dict:
        """GetJobDetailRequest — GET /api/zpgeek/jobapp/geek/job/querydetail (key=secretKey).
        Takes the securityId from a search result; response has jobBaseInfo/bossBaseInfo/brandComInfo."""
        params = {"securityId": security_id, "lid": lid, "expectId": expect_id, "query": query}
        if extra:
            params.update(extra)
        return self.call_plain("GET", "/api/zpgeek/jobapp/geek/job/querydetail", params, key=self._secret())

    # NOTE: geek F1 recommend (GET /api/zpgeek/app/geek/recommend/joblist) is intentionally NOT
    # exposed. The app only ever calls it wrapped as a `jobListRequest` subReq inside
    # GeekF1GetListBatchRequest → POST /api/batch/batchRunV2; called standalone (even with the full
    # z10.b.g param set expectId/encryptExpectId/page/pageSize/sortType/expectPosition/filterParams)
    # the server rejects it with code 71 参数格式异常. Reproducing the batchRunV2 envelope is future work.

    def cities(self) -> dict:
        """Get1004CityRequest — GET /api/zpCommon/config/city (key=None, whitelisted). Full city
        code/name tree (city / citylv2 / hotCity)."""
        return self.call_plain("GET", "/api/zpCommon/config/city", {}, key=None)

    def add_friend(self, security_id: str, job_id: str | None = None, lid: str | None = None,
                   expect_id: str | None = None, friend_id: str | None = None,
                   apply_job_directly: int = 1, similar_position: str = "0",
                   start_chat_exp_group: int = 0, extra: dict | None = None) -> dict:
        """GeekCreateFriendRequest — GET /api/zpgeek/app/friend/add (key=secretKey). WRITE: starts a
        chat / 打招呼. Verified live (code 0). The params mirror the app's ht.b.k builder:
          * security_id / lid MUST come from the SEARCH-CARD (parse_jobs securityId/lid), NOT job_detail
          * friend_id = the boss's numeric userId (parse_jobs 'userId')
          * apply_job_directly defaults to 1 (the app always sends 1); entrance/jobAddressId are unused."""
        params = {"securityId": security_id, "jobId": job_id, "lid": lid,
                  "expectId": expect_id, "friendId": friend_id,
                  "applyJobDirectly": str(apply_job_directly),
                  "similarPosition": similar_position,
                  "startChatProcessExpGroup": str(start_chat_exp_group)}
        if extra:
            params.update(extra)
        if not self._secret():
            raise ValueError("打招呼需要当前账号 secretKey")
        return self.call_plain("GET", "/api/zpgeek/app/friend/add", params, key=self._secret(),
                               safe_write=True)

    # ---- captured chat-image upload, not chat-message sending ----
    def upload_image(self, path: str, security_id: str | None = None, *,
                     precheck: bool = True, original: bool = False) -> dict:
        """Upload an image and return code/message/zpData plus local `_upload` metadata.

        App v14.180: quicklyUpload (MD5 precheck) -> uploadSingle (multipart). Both sign
        with the account secretKey. Only sp/sig/app_id go on the wire, never raw strD.
        A hit reuses url/tinyUrl without sending file bytes. No chat/IM message is sent.
        `security_id` is from the upload/chat context, not an arbitrary search-card id.
        """
        from .upload import prepare_image
        image = prepare_image(path, original=original)
        return self._upload_prepared(image, security_id, precheck=precheck)

    def upload_file(self, path: str, security_id: str | None = None, *,
                    precheck: bool = True, original: bool = False) -> dict:
        """Compatibility alias for upload_image; images only, no guessed route/field overrides."""
        return self.upload_image(path, security_id, precheck=precheck, original=original)

    def _upload_prepared(self, image, security_id: str | None = None, *, precheck: bool = True) -> dict:
        """Use the same immutable bytes for precheck and upload (also used by CLI preview)."""
        from .upload import (UploadError, check_upload_response, has_image_urls,
                             required_text, upload_envelope)
        key = required_text(self.s.get("secretKey"), "session.secretKey")
        required_text(self.s.get("t2"), "session.t2")
        required_text(self.s.get("uniqid"), "session.uniqid")
        if not isinstance(self.s.get("client_info"), (str, dict)) or not self.s["client_info"]:
            raise ValueError("上传需要 session.client_info")
        defaults = self.s.get("upload_defaults") or {}
        if not isinstance(defaults, dict):
            raise ValueError("session.upload_defaults 应为 JSON 对象")
        context = required_text(security_id if security_id is not None else defaults.get("securityId"),
                                "上传上下文 securityId")
        app_id = str(self.s.get("app_id", "1003"))
        business = {"securityId": context, "source": "chat_file"}

        def post(path, fields, *, multipart=False):
            params = {**self._outer_params(), **fields}
            envelope = upload_envelope(params, path, key, app_id)
            headers = self._headers()
            if multipart:
                # Captured text parts have no filename or Content-Type; each part includes
                # its own Content-Length. Keep the observed sp/sig/app_id/file ordering.
                parts = [(k, (None, v, None, {"Content-Length": str(len(v.encode("utf-8")))}))
                         for k, v in envelope.items()]
                parts.append(("file", (image.filename, image.data, "application/octet-stream",
                                       {"Content-Length": str(len(image.data))})))
                body_args = {"files": parts}
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body_args = {"data": envelope}
            try:
                response = self._http("POST", UPLOAD_HOST + path, **body_args, headers=headers,
                                      retries=0, allow_redirects=False)
            except requests.HTTPError as exc:
                # Also surface business code=7 when wrapped in HTTP 401/403. A requests
                # Response is falsy on 4xx: test `is not None`, not its truth value.
                if exc.response is not None:
                    try:
                        error_body = self._decode_resp(exc.response.content, key)
                    except Exception:
                        pass
                    else:
                        self.raise_for_auth(error_body)
                raise
            if 300 <= response.status_code < 400:
                raise UploadError("上传接口返回重定向，已停止以避免泄露凭据或重复上传")
            # requests automatically decompresses HTTP gzip before exposing .content.
            try:
                decoded = self._decode_resp(response.content, key)
            except Exception as exc:
                raise UploadError("无法解码上传响应；结果未知，请先核实，勿直接重试") from exc
            return self.raise_for_auth(decoded)

        def result(response, *, cached):
            return {**response, "_upload": {**image.summary(), "cached": cached,
                    "stage": "cache_hit" if cached else "uploaded"}}

        if precheck:
            response = post(UPLOAD_PRECHECK_PATH, {**business, "gid": "", "fileMd5": image.md5,
                                                  "fileSize": str(len(image.data))})
            data = check_upload_response(response, "图片预检")
            if has_image_urls(data):
                return result(response, cached=True)
            if data:
                raise UploadError("图片预检返回了不完整的缓存信息，已停止，不会继续上传")
        response = post(UPLOAD_IMAGE_PATH, {**business, "fileType": "0", "multipartType": "0"},
                        multipart=True)
        data = check_upload_response(response, "图片上传")
        if not has_image_urls(data):
            raise UploadError("上传响应缺少 url/tinyUrl；不能确认上传结果，请勿直接重试")
        return result(response, cached=False)

    @staticmethod
    def parse_expects(whoami_resp: dict) -> list[dict]:
        """Pull [{expectId, encryptExpectId, positionName, salary, location}] out of a whoami response."""
        data = whoami_resp.get("zpData", whoami_resp) or {}
        detail = data.get("geekDetail") or data.get("geekDetailResponse") or data
        out = []
        for key in ("expectPositionList", "expectInternList", "expectList"):
            for e in (detail.get(key) or []):
                out.append({
                    "expectId": e.get("expectId"),
                    "encryptExpectId": e.get("encryptExpectId"),
                    "positionName": e.get("positionName") or e.get("positionType"),
                    "salary": e.get("salaryDesc") or e.get("salary"),
                    "location": e.get("locationName") or e.get("location"),
                    "kind": key,
                })
        return out

    @staticmethod
    def cardlist_data(resp: dict) -> dict:
        """The inner cardlist zpData (holds cardList, nlpFilters, labelFilter, sorts, searchLid …)."""
        card = resp.get("zpData", {}).get(CARDLIST_PATH, {})
        return card.get("zpData", {}) or {}

    @staticmethod
    def parse_jobs(resp: dict) -> list[dict]:
        zp = BossClient.cardlist_data(resp)
        jobs = []
        for cl in zp.get("cardList", []) or []:
            jobs += cl.get("positionSearchCardList", []) or []
        if not jobs and isinstance(zp.get("jobList"), list):
            jobs = zp["jobList"]
        out = []
        for j in jobs:
            pn = j.get("positionName")
            out.append({
                "jobId": j.get("jobId") or j.get("encryptJobId"),
                "encryptJobId": j.get("encryptJobId"),
                "name": pn.get("name") if isinstance(pn, dict) else (pn or j.get("jobName")),
                "salary": j.get("salaryDesc"),
                "company": j.get("brandName") or j.get("company"),
                "city": j.get("cityName") or j.get("city"),
                "area": j.get("areaDistrict") or j.get("businessDistrict"),
                "experience": j.get("jobExperience"),
                "degree": j.get("jobDegree"),
                "labels": j.get("jobLabels") or j.get("skills"),
                "hr": (j.get("bossName") or j.get("name")),
                "hrTitle": j.get("bossTitle"),
                "userId": j.get("userId"),          # boss's numeric id — friendId for `boss chat`
                "securityId": j.get("securityId"),
                "lid": j.get("lid"),
            })
        return out
