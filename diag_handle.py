"""One-off diagnostic: why isn't a handle's live detected? Writes diag_out.json (no secrets in it)."""
import json, os, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

HANDLE = sys.argv[1] if len(sys.argv) > 1 else "@24LIVEVENT"
KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
API = "https://www.googleapis.com/youtube/v3/"
out = {"handle": HANDLE, "ts": int(time.time())}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf8", "ignore")


def api(path, **p):
    return json.loads(get(API + path + "?" + urllib.parse.urlencode(dict(p, key=KEY))))


try:
    ch = api("channels", part="id,snippet", forHandle=HANDLE)["items"][0]
    cid = ch["id"]
    out["channel"] = {"id": cid, "title": ch["snippet"]["title"]}
except Exception as e:
    out["channel_error"] = str(e)[:200]
    cid = None

if cid:
    try:
        root = ET.fromstring(get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid))
        rss = [e.text for e in root.iter("{http://www.youtube.com/xml/schemas/2015}videoId")]
        out["rss_ids"] = rss[:8]
    except Exception as e:
        out["rss_ids"], out["rss_error"] = [], str(e)[:200]
    try:
        d = api("playlistItems", part="contentDetails", maxResults=8, playlistId="UU" + cid[2:])
        out["uploads_ids"] = [i["contentDetails"]["videoId"] for i in d.get("items", [])]
    except Exception as e:
        out["uploads_ids"], out["uploads_error"] = [], str(e)[:200]
    try:
        s = api("search", part="snippet", channelId=cid, eventType="live", type="video", maxResults=5)
        out["search_live"] = [(i["id"]["videoId"], i["snippet"]["title"][:80]) for i in s.get("items", [])]
    except Exception as e:
        out["search_error"] = str(e)[:200]
    ids = list(dict.fromkeys(out.get("rss_ids", []) + out.get("uploads_ids", []) + [x[0] for x in out.get("search_live", [])]))
    if ids:
        v = api("videos", part="snippet,liveStreamingDetails,status", id=",".join(ids))
        out["videos"] = [{"id": i["id"], "title": i["snippet"]["title"][:80], "liveBroadcastContent": i["snippet"].get("liveBroadcastContent"),
                          "actualStart": i.get("liveStreamingDetails", {}).get("actualStartTime"),
                          "actualEnd": i.get("liveStreamingDetails", {}).get("actualEndTime"),
                          "scheduled": i.get("liveStreamingDetails", {}).get("scheduledStartTime"),
                          "privacy": i["status"].get("privacyStatus"), "embeddable": i["status"].get("embeddable")} for i in v.get("items", [])]
print(json.dumps(out, ensure_ascii=False, indent=1))
json.dump(out, open("diag_out.json", "w"), ensure_ascii=False, indent=1)
