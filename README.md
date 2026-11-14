# YouTube Live Mirror — 100% GitHub Actions (no Render load, no Turso)

```
Dashboard (Render, only edits 2 tiny JSON files via GitHub API)
        │  ytm_config.json  (channels, keywords, ON/OFF, commands)   [branch: ytm-data]
        ▼
GitHub Actions  yt-mirror.yml  ── ytm_runner.py ──────────────────────────────
   every ~60s: RSS + 1 videos.list call -> who is live?
   live found: yt-dlp -> ffmpeg -c copy -> YouTube RTMP (your channel)
   match ends: stop -> next live (channel-list order) -> ...  one match at a time
        │  ytm_state.json  (live list, relay, history)               [branch: ytm-data]
        ▼
Dashboard reads state (a few KB per refresh, only while the YouTube tab is open)
```
Render never runs a thread, never touches Turso for this, never sees video or YouTube API traffic.
The only Render cost is the tiny GitHub API call when you open/click in the YouTube tab.

## 1. Render env (one var)
| Name | What |
|---|---|
| existing GitHub token | no new var needed: uses `YTM_GH_TOKEN` or `GH_TOKEN` or `GITHUB_TOKEN` or `DESI_GH_TOKEN` (whichever is already set; needs `repo` + `workflow` scope) |

## 2. GitHub repo secrets (Settings -> Secrets and variables -> Actions)
| Name | What |
|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API v3 key |
| `YT_STREAM_KEY` | stream key of YOUR channel (use a reusable/persistent key) |
| `YT_COOKIES` | optional cookies.txt, only if YouTube blocks the Actions IP |

## 3. Dashboard (YouTube tab)
1. Add 15-20 channels — **order = priority**.
2. Keywords `tennis, cricket` · Check every 60s · Active window (IST) e.g. 10 to 23 · Daily cap 12h.
3. Mirror ON -> worker is dispatched immediately (otherwise cron starts it within 15 min).

## 4. How it chains (6h job limit)
Job runs ~5h50m; cron (`*/15`) keeps the next run queued behind it, so the next one starts
right after. A match in progress at the cut-over is resumed by the next run (gap ~1-2 min).
A cheap "gate" step skips the heavy steps when OFF / outside window / cap reached.
GitHub disables scheduled workflows after 60 days without repo activity — re-enable if it happens.

## 5. Test plan (2-3 hours first, ONE channel)
- [ ] Add one channel that is live now, Mirror ON -> Actions run `YT Live Mirror` starts; "Live abhi" fills.
- [ ] Relay starts; Studio stream health good; picture/sound OK (latency ~20-40s).
- [ ] Run log says `Sign in to confirm you're not a bot` -> add `YT_COOKIES`, retry.
- [ ] Match ends -> history shows ended; next live (if any) starts by itself.
- [ ] Stop button ends the relay within ~15s; toggling OFF ends the whole job.
- [ ] Check API units in UI (should stay ~1.5k/day at 60s, limit 10k) and Actions run durations.

## 6. Known limits
- New lives are found via channel RSS: can lag a few minutes behind the real start.
- Actions is meant for CI; long-running relays are a grey area of GitHub's terms (account risk).
  `ytm_runner.py` is standalone — it runs unchanged on a free Oracle VM / VPS if needed.
- **Only mirror streams you have written permission for** (copyright strike / termination risk).

## 7. Facebook Page mirror (same stream -> YouTube + Facebook)
One ffmpeg, one encode, two outputs (`tee`): whatever goes to YouTube also goes to your Facebook Page. Relay 1 only.
A Facebook failure is ignored by ffmpeg, so it can never stop the YouTube relay.
1. Facebook Page -> Meta Business Suite / Live Producer -> **Streaming software** -> copy the **persistent stream key**.
2. Repo secret `FB_STREAM_KEY` = that key. (Remove it, or set repo variable `FB_ENABLE=off`, to stop Facebook.)
3. When FB is on, video bitrate is capped at 4 Mbps (Facebook limit at 720p). State shows `fb_on` / `fb_dropped`.
4. **Relay 2 -> Facebook:** secret `FB_STREAM_KEY_2` (a second, different Facebook stream key: one key carries one live at a time, so use another Page/Live Producer key). Unset = relay 2 stays YouTube only.
