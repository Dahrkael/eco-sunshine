# "Ecovacs" APK patch 1.4.9 (com.ecovacs.ecosphere.intl)

Problem: targetSdk 26 -> the app only trusts system CAs; the bridge's CA
lives in the user store -> TLS alert 46 (certificate_unknown).

Patch: a network security config that adds user anchors:
  1. apktool d -f -o decoded-full <original-apk>
  2. manifest: <application ... android:networkSecurityConfig="@xml/nsc_bumper">
  3. new res/xml/nsc_bumper.xml (see the attached file)
  4. apktool b decoded-full -o patched-unsigned.apk
     (compile in the container's native fs: cross-filesystem builds take >10 min)
  5. sign: uber-apk-signer --allowResign --skipZipAlign
     (the bundled zipalign is glibc and dies on alpine/musl)

Result: ECOVACS_1.4.9-bumper-patched.apk (debug signature; requires
uninstalling the original app before installing this one).
