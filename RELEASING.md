# Releasing

Home Assistant's add-on store reads `openai_realtime_voice_agent/config.yaml` on
`main`: a new `version` there is an update for everyone, and `image:` tells it to
pull `ghcr.io/craig-b/{arch}-openai-realtime-voice-agent:<version>`. So the images
for a version must be published before `main` names that version. The **Add-on**
workflow's *Published images* check fails on any branch whose `config.yaml`
names images that can't be pulled.

1. **Release pull request.** Bump `version` in `config.yaml` and in
   `repository.json`, and add the `CHANGELOG.md` entry, upgrade steps first. If
   the firmware changed, bump its `project.version` in
   `firmware/home-assistant-voice.realtime.yaml`. Its *Published images* check
   fails for now: the images don't exist yet.
2. **Tag the release commit**, the pull request's head, before `main` moves:

   ```
   git tag vX.Y.Z <release commit> && git push origin vX.Y.Z
   ```

   The tag's run checks that the tag matches `config.yaml`, then builds, tests and
   pushes both architectures' images.
3. **Check the images.** When the tag's run has passed, re-run the pull request's
   checks: *Published images* passes once both images can be pulled without
   logging in. The first time a package is published it is private: make it public
   in the package's settings on GitHub (once per architecture).
4. **Merge by fast-forwarding `main`** to the release commit
   (`git push origin <branch>:main`), so the tagged commit is the one on `main`.
   For stacked pull requests, fast-forward each base branch too, so GitHub marks
   every one merged.
5. **GitHub Release** on the tag, with the changelog entry as its notes:

   ```
   gh release create vX.Y.Z --repo craig-b/ha-openai-realtime --title vX.Y.Z --notes-file <entry>
   ```
