# Parche APK "Ecovacs" 1.4.9 (com.ecovacs.ecosphere.intl)

Problema: targetSdk 26 -> la app solo confia en CAs del sistema; el CA de
Bumper esta en el almacen de usuario -> TLS alert 46 (certificate_unknown).

Parche: network security config que anade anclas de usuario:
  1. apktool d -f -o decoded-full <apk-original>
  2. manifest: <application ... android:networkSecurityConfig="@xml/nsc_bumper">
  3. nuevo res/xml/nsc_bumper.xml (ver fichero adjunto)
  4. apktool b decoded-full -o patched-unsigned.apk
     (compile in the container’s native fs: cross-filesystem builds take >10 min)
  5. firma: uber-apk-signer --allowResign --skipZipAlign
     (el zipalign bundled es glibc y muere en alpine/musl)

Resultado: ECOVACS_1.4.9-bumper-patched.apk (firma debug; requiere
desinstalar la app original antes de instalar esta).
