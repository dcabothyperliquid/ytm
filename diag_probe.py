"""One-off diagnostic: why does yt-dlp say 'Video unavailable' for a live that the API lists as live?
Runs the runner's exact probe command + variants. Writes diag_out.json (no secrets: cookie NAMES only)."""
import json, os, subprocess, sys, time, re, urllib.request

vids = [v for v in (sys.argv[1] if len(sys.argv) > 1 else "").replace(",", " ").split() if v]
out = {"ts": int(time.time()), "vids": vids}
CK = "cookies.txt"


def run(cmd, t=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
        return {"rc": p.returncode, "err": (p.stderr or "").strip()[-700:], "out_len": len(p.stdout or ""), "_out": p.stdout}
    except Exception as e:
        return {"rc": -1, "err": "%s: %s" % (type(e).__name__, str(e)[:200]), "out_len": 0, "_out": ""}


out["yt_dlp"] = run(["yt-dlp", "--version"])["_out"].strip()
out["node"] = run(["node", "--version"])["_out"].strip()
has_ck = os.path.exists(CK)
out["cookies_file"] = has_ck
if has_ck:
    names = []
    for ln in open(CK, errors="ignore"):
        f = ln.rstrip("\n").split("\t")
        if len(f) >= 7:
            names.append(f[5])
    out["cookie_names"] = sorted(set(names))[:40]
    out["cookie_count"] = len(names)

BASE = ["yt-dlp", "--no-warnings", "--no-playlist"]
EJS = ["--js-runtimes", "node", "--remote-components", "ejs:github"]
variants = {
    "runner_exact": BASE + EJS + (["--cookies", CK] if has_ck else []),
    "no_cookies": BASE + EJS,
    "no_ejs_with_cookies": BASE + (["--cookies", CK] if has_ck else []),
    "client_tv": BASE + EJS + ["--extractor-args", "youtube:player_client=tv"] + (["--cookies", CK] if has_ck else []),
    "client_mweb": BASE + EJS + ["--extractor-args", "youtube:player_client=mweb"] + (["--cookies", CK] if has_ck else []),
    "client_android": BASE + EJS + ["--extractor-args", "youtube:player_client=android"],
}
out["probe"] = {}
for v in vids:
    url = "https://www.youtube.com/watch?v=" + v
    res = {}
    for name, base in variants.items():
        r = run(base + ["-J", url], 100)
        info = {}
        if r["rc"] == 0:
            try:
                j = json.loads(r["_out"])
                info = {"is_live": j.get("is_live"), "live_status": j.get("live_status"), "has_url": bool(j.get("url")),
                        "m3u8_formats": sum(1 for f in (j.get("formats") or []) if str(f.get("protocol", "")).startswith("m3u8"))}
            except Exception as e:
                info = {"parse": str(e)[:100]}
        res[name] = {"rc": r["rc"], "err": r["err"][-300:], **info}
    # what does the watch page itself say (playabilityStatus)?
    try:
        h = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}), timeout=25).read().decode("utf8", "ignore")
        m = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"(?:,"reason":"([^"]*)")?', h)
        res["watch_page"] = {"len": len(h), "playability": m.group(1) if m else None, "reason": (m.group(2) if m else None),
                             "isLiveNow": '"isLiveNow":true' in h, "consent": "consent.youtube" in h}
    except Exception as e:
        res["watch_page"] = {"error": str(e)[:200]}
    out["probe"][v] = res
print(json.dumps(out, ensure_ascii=False, indent=1))
json.dump(out, open("diag_out.json", "w"), ensure_ascii=False, indent=1)
