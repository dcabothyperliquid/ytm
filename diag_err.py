"""One-off: like diag_bind but never crashes - records the exact YouTube/OAuth error bodies (quota? invalid_grant?) + bindings."""
import json, os, time, urllib.parse, urllib.request, urllib.error
import ytm_runner as R

out = {"ts": int(time.time())}


def err_text(e):
    try:
        return "%s %s" % (e, e.read().decode("utf8", "ignore")[:600])
    except Exception:
        return str(e)[:600]


tok = None
try:
    tok = R._oauth_token()
    out["oauth"] = "ok" if tok else "no secrets"
except Exception as e:
    out["oauth"] = "ERROR " + err_text(e)
H = {"Authorization": "Bearer " + (tok or "")}
k1, k2 = os.environ.get("YT_STREAM_KEY", "").strip(), os.environ.get("YT_STREAM_KEY_2", "").strip()


def get(path, params):
    with urllib.request.urlopen(urllib.request.Request(R._API + path + "?" + urllib.parse.urlencode(params), headers=H), timeout=25) as r:
        return json.loads(r.read())


names = {}
try:
    ls = get("liveStreams", {"part": "snippet,status,cdn", "mine": "true", "maxResults": 50}).get("items", [])
    out["streams"] = []
    for i in ls:
        sn = ((i.get("cdn") or {}).get("ingestionInfo") or {}).get("streamName", "")
        slot = "1" if k1 and sn == k1 else ("2" if k2 and sn == k2 else "none")
        names[i["id"]] = slot
        out["streams"].append({"id": i["id"][-8:], "title": i["snippet"].get("title"), "slot_key": slot,
                               "status": i["status"]["streamStatus"], "health": i["status"].get("healthStatus", {}).get("status")})
except Exception as e:
    out["streams_error"] = err_text(e)
try:
    lb = get("liveBroadcasts", {"part": "id,snippet,status,contentDetails", "mine": "true", "maxResults": 12}).get("items", [])
    out["broadcasts"] = [{"id": b["id"], "title": b["snippet"]["title"][:50], "life": b["status"]["lifeCycleStatus"],
                          "stream_slot": names.get(b["contentDetails"].get("boundStreamId"), "?"),
                          "autoStop": b["contentDetails"].get("enableAutoStop"),
                          "start": b["snippet"].get("actualStartTime"), "end": b["snippet"].get("actualEndTime")} for b in lb]
except Exception as e:
    out["broadcasts_error"] = err_text(e)
print(json.dumps(out, ensure_ascii=False, indent=1))
json.dump(out, open("diag_out.json", "w"), ensure_ascii=False, indent=1)
