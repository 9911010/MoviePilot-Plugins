---
name: emby-collection-covers
description: Diagnose and repair Emby collection posters, or maintain the MoviePilot V3 CollectionCoverSync plugin. Use for box-set artwork and recurring collection-cover failures; not for full-library movie or episode metadata scraping.
---

# Emby collection posters

The implementation lives in the `moviepilot-collection-cover` project. Find its `plugins.v3/collectioncoversync/` and README before editing; do not assume a fixed workstation path.

- Enumerate Emby `BoxSet` items and use each item's `ProviderIds.Tmdb` as a **collection** ID. Never treat it as a movie ID or guess an ID from a translated title.
- Keep valid existing posters, especially user-picked images. A missing `ImageTags.Primary` alone is not proof that artwork is absent: fetch the image endpoint.
- A 200 response and `.jpg` suffix do not establish image validity. Fully decode the bytes; HTML 404 responses have previously been stored as JPEG files. A timeout, 401/403, or 5xx is an unknown network/authentication state, not evidence that artwork should be replaced.
- Back up valid existing artwork to the plugin data directory. If it later disappears, prefer this original backup to a new TMDB poster. Do not clear cache or trigger a full-library metadata refresh as a first response.
- Prefer the official TMDB collection API when a key is configured. Otherwise inspect the actual collection page's poster element; ignore actor photos, member-movie thumbnails and backdrop `og:image` entries. Public collection pages may themselves use a single-movie-style cover: report this accurately.
- Validate a downloaded candidate, recheck the current Emby image immediately before upload, then upload base64 JPEG to `/Items/{id}/Images/Primary` and fetch/decode it again. Keep credentials in headers/configuration, out of source, URLs, logs and reports.
- Scheduled checks belong to the MoviePilot plugin's `get_service()`. Do not create a separate Codex recurring automation for the same job. Use host-managed one-shot jobs, cancellation and an overlap lock.
- Before release, run the repository tests, verify a read-only pass against the intended server, and load the plugin in the actual V3 host. Clearly distinguish mocked interface tests, live Emby integration tests and successful MP installation.
- Keep production credentials and server-specific reports out of a published plugin repository. Publishing or installing is separate from generating source and must be within the user's request.
