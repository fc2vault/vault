"""Client for skier233's local AI tagging server (media_ai_model_server / nsfw_ai_model_server).

The server itself and its models are installed separately by the user (personal-use
license; see README). We only call its HTTP API with a *path* to a video on this machine
and read back which tags were detected when:

    POST /v4/analyze/video   {"path", "frame_interval", "threshold", "return_confidence", "vr_video"}
    -> {"duration_seconds", "frame_interval_seconds",
        "frames": [{"time_seconds", "analysis": {"capabilities": {"tagging": {category: [tags]}}}}]}

Servers that don't have the v4 API get the older endpoint instead:

    POST /v3/process_video/  -> {"timespans": {category: {tag: [{"start", "end"?, "confidence"?}]}}}

Privacy: the server must be on this machine or the local network (checked), and system
proxies are never used, so nothing about the library leaves the LAN.
"""
import http.client
import ipaddress
import json
import socket
import threading
from urllib.parse import urlparse

_conns = set()
_conns_lock = threading.Lock()


class AIServerError(Exception):
    pass


class AIServerHTTPError(AIServerError):
    """The server answered, but with an HTTP error status."""

    def __init__(self, message, status):
        AIServerError.__init__(self, message)
        self.status = status


def abort_all():
    """Close in-flight requests (Ctrl+C / SIGTERM) so worker threads stop waiting."""
    with _conns_lock:
        conns = list(_conns)
    for conn in conns:
        try:
            if conn.sock is not None:
                conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()


def is_local_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            return False
    return bool(infos)


def _float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class StashAIClient:
    def __init__(self, url="http://127.0.0.1:8000", timeout=14400):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise AIServerError("invalid AI server URL: %r" % url)
        if not is_local_host(parsed.hostname):
            raise AIServerError("refusing AI server %s: it must run on this machine or your "
                                "local network" % parsed.hostname)
        self.url = url.rstrip("/")
        self._https = parsed.scheme == "https"
        self._host = parsed.hostname
        self._port = parsed.port
        self._base = parsed.path.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, payload=None, timeout=None):
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=timeout or self.timeout)
        body = headers = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        with _conns_lock:
            _conns.add(conn)
        try:
            conn.request(method, self._base + path, body=body, headers=headers or {})
            resp = conn.getresponse()
            status, raw = resp.status, resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise AIServerError("cannot reach AI server at %s: %s" % (self.url, e))
        finally:
            with _conns_lock:
                _conns.discard(conn)
            conn.close()
        if status >= 400:
            detail = raw.decode("utf-8", "replace")[:300]
            raise AIServerHTTPError("AI server %s %s -> HTTP %d: %s" % (method, path, status, detail),
                                    status)
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except ValueError:
            raise AIServerError("AI server returned non-JSON for %s" % path)

    def check(self):
        """Raise AIServerError if nothing is listening or the server has no model loaded.

        Any HTTP answer counts as alive; the model check only applies when /v4/health
        reports its counters (older servers don't).
        """
        try:
            health = self._request("GET", "/v4/health", timeout=10)
        except AIServerHTTPError:
            return None
        if isinstance(health, dict) and health.get("loaded_pipeline_count") == 0:
            raise AIServerError("AI server at %s is running but has no AI model loaded (download a "
                                "tagging model and activate it, then restart the server)" % self.url)
        return health

    def process_video(self, path, frame_interval=2.0, threshold=0.5):
        payload = {"path": path, "frame_interval": frame_interval, "threshold": threshold,
                   "return_confidence": True, "vr_video": False}
        try:
            return parse_v4(self._request("POST", "/v4/analyze/video", payload))
        except AIServerHTTPError as e:
            if e.status not in (404, 405):
                raise
        return parse_v3(self._request("POST", "/v3/process_video/", payload))


def _tag_items(value):
    """(tag, confidence) pairs from a tag list: "Tag", ["Tag", 0.8] or {"tag"|"name"|"label", "confidence"|"score"}."""
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, str):
            yield item, None
        elif isinstance(item, (list, tuple)) and item and isinstance(item[0], str):
            yield item[0], _float(item[1]) if len(item) > 1 else None
        elif isinstance(item, dict):
            name = item.get("tag") or item.get("name") or item.get("label")
            if isinstance(name, str):
                yield name, _float(item.get("confidence", item.get("score")))


def parse_v4(res):
    """Normalize a v4 /analyze/video response into {"spans", "frame_interval", "duration"}.

    Every detection becomes a one-frame span; moments.key_moments merges consecutive frames.
    """
    if not isinstance(res, dict) or not isinstance(res.get("frames"), list):
        raise AIServerError("unexpected AI server response (no frames)")
    spans = []
    errored = 0
    for fr in res["frames"]:
        if not isinstance(fr, dict):
            continue
        if fr.get("errors"):
            errored += 1
        t = _float(fr.get("time_seconds", fr.get("frame_index")))
        if t is None:
            continue
        analysis = fr.get("analysis") if isinstance(fr.get("analysis"), dict) else {}
        capabilities = analysis.get("capabilities") if isinstance(analysis.get("capabilities"), dict) else {}
        tagging = capabilities.get("tagging") if isinstance(capabilities.get("tagging"), dict) else {}
        for category, value in tagging.items():
            for tag, conf in _tag_items(value):
                spans.append({"category": category, "tag": tag, "start": t, "end": None,
                              "confidence": conf})
    if res["frames"] and errored == len(res["frames"]):
        raise AIServerError("AI server failed on every frame (see its log)")
    return {"spans": spans, "frame_interval": _float(res.get("frame_interval_seconds")),
            "duration": _float(res.get("duration_seconds"))}


def parse_v3(res):
    """Normalize a v3 /process_video/ response into {"spans", "frame_interval", "duration"}."""
    if isinstance(res, dict) and "timespans" not in res and isinstance(res.get("result"), dict):
        res = res["result"]
    if not isinstance(res, dict) or not isinstance(res.get("timespans"), dict):
        raise AIServerError("unexpected AI server response (no timespans)")
    spans = []
    for category, tags in res["timespans"].items():
        if not isinstance(tags, dict):
            continue
        for tag, frames in tags.items():
            for fr in frames or []:
                if not isinstance(fr, dict) or _float(fr.get("start")) is None:
                    continue
                spans.append({"category": category, "tag": tag, "start": _float(fr["start"]),
                              "end": _float(fr.get("end")), "confidence": _float(fr.get("confidence"))})
    return {"spans": spans, "frame_interval": _float(res.get("frame_interval")),
            "duration": _float(res.get("duration"))}
