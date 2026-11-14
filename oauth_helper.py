"""One-off helper: mode 'url' -> consent URL; mode 'exchange' -> auth code -> refresh token (encrypted to oauth_pub.b64, never printed)."""
import base64, json, os, sys, urllib.parse, urllib.request, urllib.error

CID = os.environ.get("YT_CLIENT_ID", "").strip()
SEC = os.environ.get("YT_CLIENT_SECRET", "").strip()
REDIR = "https://developers.google.com/oauthplayground"
SCOPE = "https://www.googleapis.com/auth/youtube"
out = {}


def post(url, data):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=urllib.parse.urlencode(data).encode()), timeout=25) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": "%s %s" % (e.code, e.read().decode("utf8", "ignore")[:300])}


mode = sys.argv[1]
if mode == "url":
    out["client_id_tail"] = CID[-30:]
    out["url"] = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": CID, "redirect_uri": REDIR, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent"})
else:
    code = urllib.parse.unquote(sys.argv[2].strip())
    if "code=" in code:                                   # whole URL pasted
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code", [code])[0]
    tk = post("https://oauth2.googleapis.com/token", {"code": code, "client_id": CID, "client_secret": SEC,
                                                      "redirect_uri": REDIR, "grant_type": "authorization_code"})
    rt = tk.get("refresh_token")
    out["got_refresh_token"] = bool(rt)
    out["scope"] = tk.get("scope")
    if not rt:
        out["error"] = tk.get("error") or "no refresh_token in response"
    else:
        from nacl.public import PublicKey, SealedBox
        box = SealedBox(PublicKey(base64.b64decode(open("oauth_pub.b64").read().strip())))
        out["enc"] = base64.b64encode(box.encrypt(rt.encode())).decode()
        acc = post("https://oauth2.googleapis.com/token", {"client_id": CID, "client_secret": SEC, "refresh_token": rt, "grant_type": "refresh_token"})
        try:
            req = urllib.request.Request("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                                         headers={"Authorization": "Bearer " + acc["access_token"]})
            ch = json.loads(urllib.request.urlopen(req, timeout=25).read())
            out["channel"] = [i["snippet"]["title"] for i in ch.get("items", [])]
        except Exception as e:
            out["channel_error"] = str(e)[:200]
json.dump(out, open("diag_out.json", "w"), indent=1)
print({k: (v if k not in ("enc", "url") else "...") for k, v in out.items()})
