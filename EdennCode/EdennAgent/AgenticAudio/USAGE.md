# Trying Agentic Audio

Agentic Audio scores your video. You upload footage, say what you want in one
line, and a director agent watches the video, proposes a musical direction,
generates real music, drafts a restrained narration, plans sound effects on the
actual cuts, and mixes it all onto your video — asking you before every step
that costs money.

Everything below was walked end-to-end on the deployed app on 2026-08-26/27.

## Getting in

Open the console with your access token in the URL:

    https://studio-app.studio.example.invalid/api/v2/agentic/audio/app/?backend=real&token=YOUR_TOKEN

No token → every action fails with 401 (an upload chip stuck on "upload
failed" is the classic symptom). If you land tokenless, the sign-in card
offers **Open settings** — paste the token there and the page reboots signed
in. Set your display name in Settings too; collaborators see it.

Tokens are issued by whoever operates the deployment (they live in the app's
`agentic-audio-api-keys` secret as `token:user_id` pairs — see
`deploy/deploy.sh`).

## The journey

1. **Attach your video.** Click the `+`, pick a file (up to 512 MB / 10
   minutes; anything from ~15 seconds up works well). The chip shows the
   measured duration when the upload succeeded.

2. **Say what you want, in your own words.** One line is enough — "Cinematic
   and premium — let the music build with the cuts, land the emotion on the
   final shot." The chips (Cinematic trailer, Upbeat reel, Add a voiceover…)
   are shortcuts for the same thing. Then **Start session**.

3. **Watch it read your footage.** The agent names the session from the video,
   cuts it into scenes (they appear on the timeline ruler), and builds a
   spotting sheet of the moments that matter. This is real analysis of your
   pixels, not a template.

4. **Pick your layers.** A picker offers Music / Voice-over / Sound effects —
   all three start selected; click a row to toggle it. Your exact selection
   becomes the plan; the agent will not quietly add layers back.

5. **Choose a direction.** The agent proposes named musical directions (e.g.
   "Velvet Emotional Arc" vs "Midnight Gala Pulse"). Pick one with **Use
   this**. A confirmation appears first — *"This creates the music … it spends
   to generate"* — because from here real provider credits are spent. Takes
   arrive in about 90 seconds; listen, then lock the one you want.

6. **Talk to it like a director.** Everything else happens in chat:
   - *"Add a short voice-over — restrained, only where it helps."* It drafts
     timed lines against your cuts (and deliberately holds moments silent —
     coverage is not the goal), then records on your approval through the
     narration engine. Five preset voices; delivery follows your direction.
   - *"Plan sound effects"* — hits land on the actual cuts, with a treatment
     card you approve first.
   - *"Mix it down"* — music ducked under narration, effects bedded, one
     deliverable on your video.

7. **See it on the timeline.** The timeline toggle shows lanes for music,
   narration segments, effects, and the spotting sheet against the video.
   Click a lane to seek. Export gives you the finished video.

8. **Share it.** Share from the session menu creates a link with a signed
   grant and a role — editor, commenter, or viewer. The recipient signs in as
   themselves, sees a join card, and lands in the same live session; comments
   and turns sync in real time.

Your sessions list persists — reload, come back later, rename, or delete.
Sessions survive server restarts (they live in the product's own database).

## Demo caveats, honestly

- **Finish a session in one sitting.** Generated audio/video files are not yet
  durable across app redeploys — after one, an old session opens but can't
  continue or replay its media. (Known blocker; fix planned.)
- **Generation spends real credits** — that's what the confirmation gates are
  for. Nothing spends without your click.
- One session runs one turn at a time; a second person's turn waits its turn
  rather than colliding.
- The first analysis takes ~30–60s on a 16s clip; music ~90s; narration a few
  seconds.

## For operators

Provision/migrate/deploy scripts live in `EdennCode/EdennAgent/AgenticAudio/deploy/`
— see each script's header. The stack is fully isolated (own resource group,
database, registry, container environment). To add a user: append
`newtoken:their_id` to the `agentic-audio-api-keys` secret and restart the
revision.
