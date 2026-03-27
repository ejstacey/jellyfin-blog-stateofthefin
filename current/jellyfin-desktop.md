---
client_name: Jellyfin Desktop
client_url: https://github.com/jellyfin/jellyfin-desktop
author_name: Andrew Rabert
author_url: https://github.com/andrewrabert
---

#### What's New

Jellyfin Desktop v2.0 was announced and some intial packaging was done for a couple platforms. It is built upon Qt libraries, like Jellyfin Media Player (v1 of the same software) was. Around that same time a [severe memory leak](https://github.com/jellyfin/jellyfin-desktop/issues/1091) in the Qt libraries was discovered. This halted all improvements/packaging. To date, this issue still exists in the Qt libraries.

As an alternative, work was started on an SDL/CEF version. This version (which we're going to call Jellyfin Desktop v3) is fairly functional and ready for testing. We would appreciate user testing and feedback to help complete and polish the first version of v3! You can find the updated version at the [jellyfin-cef repository](https://github.com/jellyfin-labs/jellyfin-desktop-cef).

This project is also being done as an experiment with using Claude to migrate from the old codebase. This migration is being done by someone who worked extensively on previous versions and is not just using Claude to create "AI slop". See elsewhere in this blog post for our discussion on AI.

Please see the [CEF issues page](https://github.com/jellyfin-labs/jellyfin-desktop-cef/issues) for current issues/workarounds with the new version.

#### What's Next

The jellyfin/jellyfin-desktop repository will be renamed to jellyfin/jellyfin-desktop-qt, and the jellyfin-labs/jellyfin-desktop-cef repository will be moved to jellyfin/jellyfin-desktop (where the old Qt-library-version originally was), so if you're reading this blog post long after it originally comes out, you may find the repositories referenced here have changed.

Besides that, we are continuing work and improvements on the new version.
