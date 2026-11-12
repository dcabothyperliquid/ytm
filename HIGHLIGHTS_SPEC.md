# Highlights — decided plan + status (handoff doc)

Owner: Naem. Repos: `dcabothyperliquid/ytm` (worker, GitHub Actions) + `dcabothyperliquid/Xbot` (dashboard on Render).
Never put tokens/cookies in this repo (public). Secrets live in GitHub Actions secrets only.

## 1. Flow (DECIDED, built)
1. **Scout** (cheap, audio only): bot picks a finished stream VOD, finds match **start = toss** and **finish = last wicket / winning run + ~45 s celebration** (`hl_scout.py`).
2. **Complete-match rule**: highlights ONLY if the VOD has the toss at the start AND the winning run/result at the end AND 2nd-innings signs (target / need X runs). A 4-5 min hole in the middle is OK. Otherwise status `incomplete` (card "MATCH POORA NAHI LAGA"), highlights only if Naem presses "Fir bhi banao".
3. **Dashboard card (Highlights tab, "Step 1")**: start/finish editable, "YouTube pe dekho" opens the VOD at that second, buttons Confirm / Dobara dhoondho / Skip.
4. **Confirm** -> worker makes highlights + shorts ONLY from `[start, end]` (`hl_review.do_confirm`, `hl_runner.run_vid(window=...)`). Times in state/report are original-VOD times (offset added), so Edit/Re-make keep working.
5. Preview + Approve in dashboard -> upload to YouTube (existing).

Commands (hl_config.json `commands`): `confirm {vid,start,end}`, `skip {vid}`, `rescout {vid}`, plus old `edit`, `rerun` (rerun reuses the stored window).

## 2. How the highlights video must be CUT (DECIDED, NOT built yet)
Today the maker is still the old audio-peak + keyword "moments" ranker. The agreed cut:

1. **Toss part**: banner / who is playing / toss result and who bats or bowls first, with the commentary playing UNINTERRUPTED (no audio chopped in the middle). If toss and decision are far apart, cut the gap (about 30 s of footage is fine), never cut mid-sentence.
2. **Every over, in order**: show every four, six, wicket, no-ball, wide. Dot/single balls only as filler: if 3 balls of the over are already shown, don't add dots; if a dot is needed show the over's first, middle or last ball.
3. **Dot balls in the death overs (last 3-4 overs)** are kept when they change the match (tight chase), not treated as filler.
4. **Six**: clear commentary of the whole six; replay only for a few big sixes (3-5), not all.
5. **Wicket / six / four are never missed.** If the reel gets too long, drop fours first, never wickets/sixes.
6. **End**: last over complete, winning run / last wicket, then 30-60 s celebration.
7. Only for matches that went to the last over (2nd innings completed).

Needs a ball-by-ball timeline (which ball was dot/wide/no-ball/four/six/wicket). Local matches have no data API, so the source must be the on-screen scoreboard (OCR of score/overs per frame) and/or commentary. Not built.

## 3. Open work (in order)
1. Section download (`--download-sections`) was flaky on the 8 h VOD `L2RKN7yGSdM` (frames at different times came back identical / some failed). Switch `run_vid` to full download (<=720p) + `ffmpeg -ss/-to` trim of the confirmed window. Watch runner disk (~14 GB free).
2. Check whether these streams show a scoreboard overlay (frames so far showed LML SPORTS / KCC logos and sponsor slates only; no scoreboard seen yet). Diag workflow: `yt-diag-frames.yml` (pushes only jpg + info.json to branch `ytm-frames`, never cookies).
3. Ball-by-ball timeline from scoreboard OCR -> over-by-over cutter per section 2.
4. Worth-making check right after the match ends (see section 4).

## 4. "Is this match worth making highlights for" (idea, partly built)
Built: the scout complete-match rule above (toss + result + 2nd innings) runs automatically when a stream ends, so unfinished matches are never processed by default.
Idea, not built: a cheap score before any download — result margin (close finish = better), last-over excitement from audio energy, and how many wickets/sixes the commentary mentions — shown on the Step-1 card as "worth: high/medium/low" so Naem confirms only good matches.

## 5. Big files (>1 GB)
- Everything heavy runs on GitHub Actions (free runner ~14 GB disk), never on Render.
- Full-quality outputs go to the DRAFT release `hl-<vid>` (one asset must stay under 2 GB; the highlights file is ~10 min so it is far below that). Previews are small and public (`hlp-<vid>`).
- Approved items upload straight from Actions to YouTube; nothing needs to be downloaded to the phone.

## 6. Commits so far
ytm: 1882625 (scout + confirm flow), c2e9528 (toss start / last ball + 45 s), a24abc1 (toss + result required), diag frames workflow.
Xbot: a253458 (Step-1 confirm card, confirm|skip|rescout actions).
Render for Xbot needs a Manual Deploy to show the new card.
