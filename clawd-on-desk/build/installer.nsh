!macro customInit
  nsExec::Exec 'taskkill /F /IM llama-server.exe /T'
  nsExec::Exec 'taskkill /F /IM minicpm-sidecar.exe /T'
!macroend

!macro customInstall
  SetOutPath "$INSTDIR"
  File "/oname=$INSTDIR\uninstall-claude-hooks.ps1" "${BUILD_RESOURCES_DIR}\uninstall-claude-hooks.ps1"
  FileOpen $0 "$INSTDIR\.clawd-install-user-home" w
  FileWrite $0 "$PROFILE"
  FileClose $0

  ; Trust the signing certificate. electron-updater verifies the Authenticode
  ; signature of every downloaded installer (verifyUpdateCodeSignature defaults
  ; to true), and it does that by building a chain to a trusted root. A
  ; self-signed cert is trusted nowhere until something puts it in a store, so
  ; without this line the very first update a user attempts fails with
  ; ERR_UPDATER_INVALID_SIGNATURE - permanently, because the thing that would
  ; fix it is the update that cannot install.
  ;
  ; -user throughout: CurrentUser needs no administrator rights, and a dev
  ; certificate has no business in the machine-wide store. certutil is used
  ; rather than a plugin because it ships with every Windows install.
  ;
  ; The .cer is the public half only. The matching private key never leaves the
  ; machine that generated it, which is what makes signing possible at all.
  !if /FileExists "${BUILD_RESOURCES_DIR}\deskpet-selfsigned.cer"
    File /oname=$PLUGINSDIR\deskpet-selfsigned.cer "${BUILD_RESOURCES_DIR}\deskpet-selfsigned.cer"
    File /oname=$PLUGINSDIR\untrust-cert.ps1 "${BUILD_RESOURCES_DIR}\untrust-cert.ps1"
    nsExec::ExecToLog 'certutil -user -addstore -f Root "$PLUGINSDIR\deskpet-selfsigned.cer"'
    Pop $0
    nsExec::ExecToLog 'certutil -user -addstore -f TrustedPublisher "$PLUGINSDIR\deskpet-selfsigned.cer"'
    Pop $0
    ${If} $0 != 0
      DetailPrint "Could not add the DeskPet signing certificate to your trust store."
      DetailPrint "The app will run, but auto-update will refuse to install a new version."
    ${EndIf}
  !endif

  ; Install the x64 VC++ Runtime if missing (needed by the native llama-server).
  ; x64 only per decision D3. This used to reference vc_redist.arm64.exe, which
  ; was the only redist the build ever downloaded - so x64 users never got the
  ; runtime they needed and were handed a useless arm64 one instead.
  !if /FileExists "${BUILD_RESOURCES_DIR}\vc_redist.x64.exe"
    ; Probe the real file, not $SYSDIR\vcruntime140.dll. That check is satisfied
    ; by any vcruntime on the machine regardless of version or architecture, so
    ; an old or foreign-architecture DLL silently skipped the install.
    IfFileExists "$SYSDIR\msvcp140.dll" vcrt_done 0
      File /oname=$PLUGINSDIR\vc_redist.x64.exe "${BUILD_RESOURCES_DIR}\vc_redist.x64.exe"
      ExecWait '"$PLUGINSDIR\vc_redist.x64.exe" /install /quiet /norestart'
    vcrt_done:
  !endif
!macroend

!macro customUnInstall
  nsExec::Exec 'taskkill /F /IM llama-server.exe /T'
  nsExec::Exec 'taskkill /F /IM minicpm-sidecar.exe /T'

  ; Drop the trust anchor the installer added. It is what makes the auto-updater
  ; accept a signed build, so leaving it behind would outlive the app. Shipped
  ; as a script file rather than an inline powershell -Command, because nested
  ; quoting inside an NSIS string is how this silently stops running.
  !if /FileExists "$PLUGINSDIR\untrust-cert.ps1"
    nsExec::ExecToLog 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\untrust-cert.ps1"'
    Pop $0
  !endif

  StrCpy $1 "$PROFILE"
  IfFileExists "$INSTDIR\.clawd-install-user-home" 0 clawd_node_cleanup_home_done
    FileOpen $0 "$INSTDIR\.clawd-install-user-home" r
    FileRead $0 $1
    FileClose $0
  clawd_node_cleanup_home_done:

  StrCpy $2 "$INSTDIR\Deskpet.exe"
  IfFileExists "$2" clawd_node_cleanup_have_exe 0
  StrCpy $2 "$INSTDIR\MiniCPM Desk Pet.exe"
  IfFileExists "$2" clawd_node_cleanup_have_exe 0
  ; Legacy upstream executable name, kept only so uninstall cleanup can run
  ; when a local install dir still contains older migrated artifacts.
  StrCpy $2 "$INSTDIR\Clawd on Desk.exe"
  IfFileExists "$2" clawd_node_cleanup_have_exe clawd_node_cleanup_done

  clawd_node_cleanup_have_exe:
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\hooks\cleanup-integrations.js" 0 clawd_node_cleanup_done
    System::Call 'Kernel32::SetEnvironmentVariable(t, t)i("ELECTRON_RUN_AS_NODE", "1").r0'
    nsExec::ExecToLog '"$2" "$INSTDIR\resources\app.asar.unpacked\hooks\cleanup-integrations.js" --apply --user-home "$1" --source nsis --fail-open'
    Pop $0
    System::Call 'Kernel32::SetEnvironmentVariable(t, t)i("ELECTRON_RUN_AS_NODE", "").r0'
  clawd_node_cleanup_done:

  IfFileExists "$INSTDIR\uninstall-claude-hooks.ps1" 0 clawd_uninstall_hooks_done
    nsExec::ExecToLog 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\uninstall-claude-hooks.ps1" -InstallDir "$INSTDIR"'
    Pop $0
  clawd_uninstall_hooks_done:
!macroend
