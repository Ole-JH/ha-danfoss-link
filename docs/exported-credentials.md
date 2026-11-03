# Alternative: use exported app credentials

If pairing doesn't work for you, Home Assistant can borrow the identity of an Android
install of the Danfoss Link app that is **paired with your CC**. You need three things
from it: `app_cert.pem`, `app_key.pem` and the CC peer ID. They live in the app's
private storage, so you need root access to that Android: a rooted phone or, easier,
the Android emulator on a PC/Mac.

1. **Get the app.** Install Danfoss Link from Google Play on your own phone if it is
   still listed for you, then copy the APK off it (no root needed):
   ```bash
   adb shell pm path com.danfoss.linkapp           # prints package:/data/app/.../base.apk
   adb pull /data/app/.../base.apk danfoss-link.apk
   ```
   If it is no longer available, only use an APK from a source you trust.
2. **Create an emulator** in Android Studio → Device Manager with a
   **"Google APIs"** system image (not "Google Play": Play images refuse `adb root`).
   On an Apple Silicon Mac use an **arm64-v8a** image. On an Intel/AMD PC use an
   **x86_64** image of Android 11 or newer, which can run ARM apps.
3. Start it, then `adb install danfoss-link.apk`.
4. **Pair** the app with your CC as you did on your phone. The PC must be on the same
   network as the CC. Check that your rooms show up.
5. **Export** with the script in this repository ([scripts/export_credentials.sh](../scripts/export_credentials.sh)):
   ```bash
   ./scripts/export_credentials.sh ~/danfoss_link_creds
   ```
   It prints the CC peer ID (looks like `e7:af:e3:77:...`, 20 bytes) and saves the two
   PEM files. Manual alternative: the files are in
   `/data/data/com.danfoss.linkapp/files/luadata/`, and the peer ID is in the file in
   `pairings/` (`["peer_id"]="..."`) or is that file's name with `_` instead of `:`.
6. **Close the app** (and shut the emulator down). The relay allows one connection
   per identity, so the app and Home Assistant would keep kicking each other off. Don't
   remove the pairing or re-pair the app; that invalidates the exported identity.
7. Settings → Devices & services → Add integration → **Danfoss Link** →
   **Use exported app credentials**. Paste the contents of `app_cert.pem` and
   `app_key.pem` and the peer ID. Instead of pasting, you can copy the files to your
   config folder and enter a path such as `danfoss_link_creds/app_cert.pem`. The form
   checks the key against the certificate and connects to the CC before saving.

If you re-pair the app later, use **Reconfigure** on the integration to paste the new
files.

Treat `app_key.pem` like a password: anyone with it can control your heating. The same
goes for the key Home Assistant creates when pairing, which is stored in its config entry.

If something fails, see [Troubleshooting](troubleshooting.md).
