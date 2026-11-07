"""One-off: show our broadcasts/streams, try ready->live transition, record the FULL error body (no secrets)."""
import json, time, urllib.error, urllib.parse, urllib.request
import ytm_runner as R

tok = R._oauth_token()
out = {"ts": int(time.time()), "oauth": bool(tok)}
H = {"Authorization": "Bearer " + (tok or ""), "Content-Type": "application/json"}


def call(method, path, params):
    req = urllib.request.Request(R._API + path + "?" + urllib.parse.urlencode(params),
                                 data=b"{}" if method == "POST" else None, headers=H, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"HTTP": e.code, "body": e.read().decode("utf8", "ignore")[:600]}


if tok:
    ls = call("GET", "liveStreams", {"part": "snippet,status,cdn", "mine": "true", "maxResults": 5})
    out["streams"] = [{"id": i["id"], "status": i["status"], "res": i["cdn"].get("resolution"), "fps": i["cdn"].get("frameRate")}
                      for i in ls.get("items", [])] if "items" in ls else ls
    lb = call("GET", "liveBroadcasts", {"part": "id,snippet,status,contentDetails", "mine": "true", "maxResults": 6})
    items = lb.get("items", []) if "items" in lb else []
    out["broadcasts"] = [{"id": i["id"], "title": i["snippet"]["title"][:50], "life": i["status"]["lifeCycleStatus"],
                          "privacy": i["status"].get("privacyStatus"), "autoStart": i["contentDetails"].get("enableAutoStart"),
                          "boundStream": i["contentDetails"].get("boundStreamId")} for i in items] if items else lb
    out["transitions"] = []
    for i in items:
        if i["status"]["lifeCycleStatus"] in ("ready", "testing"):
            out["transitions"].append({"id": i["id"], "result": call("POST", "liveBroadcasts/transition",
                                       {"broadcastStatus": "live", "id": i["id"], "part": "status"})})
print(json.dumps(out, ensure_ascii=False, indent=1))
json.dump(out, open("diag_out.json", "w"), ensure_ascii=False, indent=1)
