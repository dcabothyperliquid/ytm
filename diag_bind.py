"""One-off: which liveStream carries which slot's key (match only, key never printed) + which broadcast is bound to which stream."""
import json, os, time, urllib.parse, urllib.request
import ytm_runner as R

tok = R._oauth_token()
H = {"Authorization": "Bearer " + (tok or "")}
k1, k2 = os.environ.get("YT_STREAM_KEY", "").strip(), os.environ.get("YT_STREAM_KEY_2", "").strip()


def get(path, params):
    with urllib.request.urlopen(urllib.request.Request(R._API + path + "?" + urllib.parse.urlencode(params), headers=H), timeout=25) as r:
        return json.loads(r.read())


out = {"ts": int(time.time()), "key1_set": bool(k1), "key2_set": bool(k2), "same_key": bool(k1) and k1 == k2}
ls = get("liveStreams", {"part": "snippet,status,cdn", "mine": "true", "maxResults": 50}).get("items", [])
names = {}
out["streams"] = []
for i in ls:
    sn = ((i.get("cdn") or {}).get("ingestionInfo") or {}).get("streamName", "")
    slot = "1" if k1 and sn == k1 else ("2" if k2 and sn == k2 else "none")
    names[i["id"]] = slot
    out["streams"].append({"id": i["id"][-8:], "title": i["snippet"].get("title"), "slot_key": slot, "status": i["status"]["streamStatus"],
                           "health": i["status"].get("healthStatus", {}).get("status")})
lb = get("liveBroadcasts", {"part": "id,snippet,status,contentDetails", "mine": "true", "maxResults": 25}).get("items", [])
out["broadcasts"] = [{"id": b["id"], "title": b["snippet"]["title"][:40], "life": b["status"]["lifeCycleStatus"],
                      "stream": (b["contentDetails"].get("boundStreamId") or "")[-8:],
                      "stream_slot": names.get(b["contentDetails"].get("boundStreamId"), "?"),
                      "created": b["snippet"].get("publishedAt")} for b in lb]
print(json.dumps(out, ensure_ascii=False, indent=1))
json.dump(out, open("diag_out.json", "w"), ensure_ascii=False, indent=1)
