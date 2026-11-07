"""Channel + bot snapshot -> stats.json (no secrets). Run by .github/workflows/yt-stats.yml, published to branch ytm-stats."""
import datetime, json, time, urllib.error, urllib.parse, urllib.request
import ytm_runner as R

tok = R._oauth_token()
out = {"generated": int(time.time()), "generated_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime()), "oauth": bool(tok)}
H = {"Authorization": "Bearer " + (tok or "")}


def get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=25) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"HTTP": e.code, "body": e.read().decode("utf8", "ignore")[:300]}


if tok:
    ch = get(R._API + "channels?" + urllib.parse.urlencode({"part": "snippet,statistics", "mine": "true"}))
    it = (ch.get("items") or [None])[0]
    if it:
        s = it["statistics"]
        out["channel"] = {"id": it["id"], "title": it["snippet"]["title"], "subscribers": int(s.get("subscriberCount", 0)),
                          "views": int(s.get("viewCount", 0)), "videos": int(s.get("videoCount", 0))}
    else:
        out["channel"] = ch
    end = datetime.date.today()
    for name, days in (("last_28d", 28), ("last_365d", 365)):
        q = {"ids": "channel==MINE", "startDate": str(end - datetime.timedelta(days=days)), "endDate": str(end),
             "metrics": "views,estimatedMinutesWatched,subscribersGained,subscribersLost"}
        a = get("https://youtubeanalytics.googleapis.com/v2/reports?" + urllib.parse.urlencode(q))
        if "rows" in a and a["rows"]:
            v, m, g, l = a["rows"][0]
            out[name] = {"views": v, "watch_hours": round(m / 60.0, 1), "subs_gained": g, "subs_lost": l}
        else:
            out[name] = {"error": a.get("HTTP") or "no rows", "note": a.get("body", "")[:200]}

try:  # bot summary from the ytm-data branch (checked out next to this repo by the workflow)
    st = json.load(open("/tmp/ytm_state.json"))
    h = st.get("history", [])
    out["bot"] = {"status": st.get("status"), "units_today": st.get("units_today"),
                  "relay": (st.get("relay") or {}).get("title"),
                  "outcomes_last30": {k: sum(1 for x in h if x.get("outcome") == k) for k in {x.get("outcome") for x in h}},
                  "recent": [{"title": (x.get("title") or "")[:60], "outcome": x.get("outcome"), "note": x.get("note"),
                              "min": round(((x.get("ended") or 0) - (x.get("started") or 0)) / 60) if x.get("started") else 0}
                             for x in h[:10]]}
except Exception as e:
    out["bot"] = {"error": str(e)[:100]}

json.dump(out, open("stats.json", "w"), ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False, indent=1))
