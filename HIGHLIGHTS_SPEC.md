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

## 2. How the highlights video must be CUT (DECIDED, BUILT in hl_cutter.py — untested on a real VOD)
Built 2026-11-13: `hl_cutter.py` transcribes the whole confirmed window (faster-whisper `HL_FULL_MODEL`, default base) and builds the plan below from the commentary; `hl_runner.process()` uses it for highlights.mp4 (falls back to the old audio-peak ranker if it fails; `HL_CUT=old` forces the old one). Shorts still use the old story-based maker. Budget = target length x1.5; the must-keep set (toss, wickets, sixes, no-balls, wides, last over) is never trimmed, so the reel can run longer than the target. The agreed cut:

1. **Toss part**: banner / who is playing / toss result and who bats or bowls first, with the commentary playing UNINTERRUPTED (no audio chopped in the middle). If toss and decision are far apart, cut the gap (about 30 s of footage is fine), never cut mid-sentence.
2. **Every over, in order**: show every four, six, wicket, no-ball, wide. Dot/single balls only as filler: if 3 balls of the over are already shown, don't add dots; if a dot is needed show the over's first, middle or last ball.
3. **Dot balls in the death overs (last 3-4 overs)** are kept when they change the match (tight chase), not treated as filler.
4. **Six**: clear commentary of the whole six; replay only for a few big sixes (3-5), not all.
5. **Wicket / six / four are never missed.** If the reel gets too long, drop fours first, never wickets/sixes.
6. **End**: last over complete, winning run / last wicket, then 30-60 s celebration.
7. Only for matches that went to the last over (2nd innings completed).

Limits of the built version: events come from commentary keywords + audio energy (no scoreboard, no real over numbers). Innings break = longest no-play stretch; death overs = last 18% of each innings; replay = extra 10 s after the 4 loudest sixes (not detected visually); filler dots = one dot ball when 100 s pass with nothing shown. A scoreboard/OCR ball-by-ball timeline would make this exact (open work 2-3).

## 2b. BALL-BY-BALL CUT RULES (DECIDED 2026-11-14 after Naem watched the first real output) — the bot must follow these
Matches here are 6-7 over tennis-ball matches. One VOD holds several matches (Deoria = Match 10, Kaushambi = Match 11 ...): ONE highlights video PER match, never two matches in one file.
1. **Split matches**: a new toss block is a new match if the match number ("MATCH NO. 10" -> 11) or the two team codes on the scoreboard changed, even when the previous result banner was not read (`hl_board.ident_changed`). If no "WON BY" banner was read but a next match starts and the chase was seen, the match ends at its last live-scoreboard frame.
2. **A highlight is a list of single balls, not a stretch of play**: for every four / six / wicket / no-ball / wide show ONE short clip = bowler runs in -> shot -> the commentary of THAT ball finishes -> hard cut to the next such ball. Typical clip: six 14 s, four 12 s, wicket 16 s, wide 8 s. Never keep the camera on one ball for a minute; never skip 4-5 boundaries in a row (every four / six / wicket is shown).
3. **Clip time = the moment the ball was HIT, not when the score changed.** The scoreboard operator types the score 6-10 s late. The SIX / FOUR / WICKET graphic (standalone word in the middle of the bottom bar) appears right after the shot: clip anchor = graphic onset - 2 s (`hl_scoreboard.align_events`). Events without a graphic use the median score->ball delay measured on the others.
4. **Four / six are accepted from the score ONLY when exactly one legal ball passed** (score +4/+5/+6/+7 with ball counter +1). The operator sometimes updates the score once per 2-3 balls: such a jump is NOT a six. A graphic with no score event next to it still creates an event (it is the proof of that ball). Same graphic again within 20 s = replay, ignored.
5. Dot / single balls: only as filler, and in the last 2 overs when the chase is tight (unchanged).
6. **Toss part**: first ~90 s uninterrupted (unchanged, worked well).
7. **End**: last over complete (from ~25 s before its first ball), winning run, then **45 s** of celebration. NOT 2-3 minutes of crowd (CELEBRATION / RESULT_TAIL = 45 s, finish block max 240 s).
8. Length control order: filler dots -> fours (quietest first) -> death dots -> replays -> plain wides. Never wickets, sixes, no-balls.
9. Replays: only for the 3 loudest sixes.

## 3. Open work (in order)
1. Section download (`--download-sections`) was flaky on the 8 h VOD `L2RKN7yGSdM` (frames at different times came back identical / some failed). Switch `run_vid` to full download (<=720p) + `ffmpeg -ss/-to` trim of the confirmed window. Watch runner disk (~14 GB free).
2. Check whether these streams show a scoreboard overlay (frames so far showed LML SPORTS / KCC logos and sponsor slates only; no scoreboard seen yet). Diag workflow: `yt-diag-frames.yml` (pushes only jpg + info.json to branch `ytm-frames`, never cookies).
3. (Optional, more exact) Ball-by-ball timeline from scoreboard OCR to replace the commentary-based event list in hl_cutter.
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
