# Viszmo desktop flow

The desktop app is an Electron dashboard. It launches and closes a dedicated
Google Chrome profile, then injects the Viszmo control panel inside the managed
Chrome page through the browser's private DevTools Protocol. The panel is
re-injected when the user navigates or opens another tab in that managed
Chrome window, and its commands/events stay on the same CDP channel. The Python agent
runs as a local sidecar. It never moves the OS cursor, sends keyboard events to
the desktop, or locks system input.

## Run it

```text
npm install
npm start
```

For the temporary Python wrapper, `python desktop_app.py` starts the same
Electron development app.

Supported browser values are `auto`, `chrome`, `edge`, and `brave`. An optional
task note can be prefilled with `--task "Finish the current assignment"`.

Click **Launch Chrome**. The first run opens a dedicated Chrome profile on
Google. Navigate to the site, sign in, and open the assignment there. The
Viszmo panel appears inside the Chrome page; use its **Run** button to start a
task. Drag the panel by its top bar to move it; its position is remembered for
that site. The dashboard's **Close Chrome** button closes the managed browser and
cancels any active run.

Every normal page tab in the managed window receives its own panel, so switching
tabs does not require refocusing the dashboard. Chrome's protected built-in New
Tab page cannot accept injected UI; when a new tab opens there, Viszmo redirects
it to Google so the panel appears immediately and the user can type the site
they want to use.

No Chrome extension is required for this managed-profile flow. An extension is
only needed later if Viszmo should control an already-open tab in the user's
normal Chrome profile without launching its own dedicated Chrome session.

## Build an installer

Install the Python build requirements, then run:

```text
pip install -r requirements-build.txt
npm run dist
```

The Electron Builder configuration targets an NSIS installer on Windows and a
DMG on macOS. The installed app checks for new GitHub releases in the
background, but does not download or restart without the user's confirmation.
The dashboard also has **Check for updates** for a manual check. When a
download finishes, the user gets a second **Restart and install** prompt.

## Release and update flow

1. Bump the desktop `version` in `package.json`.
2. Build the backend and installer with `npm run dist`. The Windows NSIS and
   macOS DMG targets produce the update metadata required by `electron-updater`.
3. Publish the installer artifacts and generated `latest.yml` metadata to a
   GitHub Release in `kortezbis/vis-saas` (or set `VISZMO_UPDATE_URL` to a
   generic HTTPS feed that serves those same files).
4. Update the website's `public/updates/viszmo.json` desktop version and deploy
   the website. The website uses the `web` entry in that same manifest to prompt
   browser users to refresh after a web release.

The website manifest is intentionally informational; the desktop updater still
validates the downloaded artifact and its signature through `electron-updater`.
For a public production build, sign the Windows installer and notarize/sign the
macOS app before publishing. Auto-updating macOS builds without code signing is
not supported.
