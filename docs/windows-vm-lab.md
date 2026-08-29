# Windows clean VM検証lab

Issue #133 / Draft PR #134のVC++ Runtime外部前提化を、GUI操作やVMware
Toolsへ依存せず検証するためのローカル専用labです。製品、インストーラー、
CI、Release工程には接続しません。

## 境界

- VMware Workstationはホスト上の電源・snapshot操作だけに使用します。
- guest操作はWindows標準のWinRMをhost-only `vmnet1`だけで使用します。
- VMware Tools、shared folder、clipboard、drag and dropを使用しません。
- guestにdefault gateway、DNS、internet接続を設定しません。
- Runtimeとpackaged appは固定SHA256のISOからだけ読み取ります。
- Runtimeをdownloadしません。Runtime DLLや`vc_redist.x64.exe`を製品へ同梱しません。
- snapshot restore、Runtime導入、VM passwordのprocess argument露出は、3個すべての
  明示switchがない限り実行しません。
- VM停止は確立済みWinRMからのguest shutdown requestだけです。失敗してもhard power offへfallbackしません。
- credential、VM、ISO、test evidenceはrepository外へ置きます。
- 実LoL録画はこのlabの対象外です。
- installer actionは固定user `vmtest`のprofileだけで実行し、native processへ渡す引数は
  空白・quoteなしの固定値へ限定します。各processは300秒でtimeoutします。

## 操作

`scripts/windows_vm_lab.ps1`は次の4操作だけを提供します。

| Action | VM変更 | 内容 |
| --- | --- | --- |
| `Capture` | なし | VMX/VMSDからUUID、MAC、暗号化方式、A0 fingerprintを取得 |
| `Plan` | なし | config、固定hash、実行順、禁止事項をJSONで表示 |
| `Doctor` | なし | VM identity、snapshot、credential、host-only、TrustedHostsをread-only確認 |
| `Run` | あり | A0復元、Environment A installer拒否、Runtime導入、Environment B installer/self-check/update/uninstall、guest shutdown |

`Run`は次の順序を固定し、途中をskipできません。

1. 対象VMが停止中であることを確認
2. 専用VMのUUID、MAC、暗号化、vTPM、NIC、ISO、isolation設定と
   `A0-runtime-absent`のUID/fingerprintを確認
3. A0へ復元して、powered-off snapshotであることを確認
4. VMを`nogui`で起動
5. WinRMでEnvironment Aを検査
6. Runtime未導入、canonical/hashed VC++ Runtime DLLなし、VMware Toolsなし、default routeなしを確認
7. 完成installerをsilent実行し、Runtime不足のexit 7、install root/registry/shortcut/user-data不変を確認
8. controlled install rootでも同じ拒否とtree不変を確認し、自作rootだけ削除
9. ISO内のMicrosoft署名済みRuntime installerを固定SHA256で検査して導入
10. Runtime versionと必要なSystem32 DLLを確認
11. ISO内の既存Environment B検査と直接app self-checkを実行
12. 完成installerを新規install、installed app self-check、上書き更新、silent uninstallの順に実行
13. uninstall後のapp/registry/shortcut消失とuser-data sentinel保持を確認
14. JSON evidenceをホストへ保存してguest shutdownを要求

Environment Aのexit 7は、Inno Setup 6.7.3の`PrepareToInstall`が続行不能と判断した場合の
公式終了コードです（[Setup Exit Codes](https://jrsoftware.org/ishelp/topic_setupexitcodes.htm)、
[PrepareToInstall](https://jrsoftware.org/ishelp/topic_scriptevents.htm)、2026-08-29確認）。
installer logはbounded textとしてJSON evidenceへ保存した後、guest profile配下の自動作成した
GUID directoryだけをpath・reparse point検査後に削除します。

## 固定test ISO

既存のIssue #133 test kitへ、このbranchで管理するbootstrapとpackaged self-check runnerを
一時stagingしてISOを作成します。
元のtest kitは変更せず、出力ISOはrepository外へ新規作成します。

```powershell
.\scripts\build_windows_vm_lab_iso.ps1 `
  -SourceKitPath .\downloads\issue133-vm-test-kit `
  -OutputPath F:\VMware\Media\lol-vc-runtime-test-pr134-managed.iso
```

builderは必要なapp、Runtime installer、Environment B script、package manifest、PE audit、wheel
provenanceが揃うことを確認し、tracked bootstrapとrunnerを追加します。stockのWindows PowerShell
5.1がguest scriptをcode page依存で誤読しないよう、直接実行する`.ps1`はUTF-8 BOM付きへ正規化します。
ISO内には主要fileのsize/SHA256を持つ
`vm-lab-media-manifest.json`も入ります。出力されたISOのSHA256だけをlocal configへ固定します。
このISOはlocal test inputであり、アプリ、installer、Release assetではありません。

## 一度だけ必要なVM準備

この準備が終わるまでは`Run`しません。

1. Windows 11 Enterprise Evaluation ISOから新しいVMを作成します。
2. VMは`F:\VMware\VMs\LoLReplayTool-VC-Runtime-Lab`へ置きます。
3. VM名、display name、directory名をすべて`LoLReplayTool-VC-Runtime-Lab`へ固定します。
4. guest OSは`windows11-64`、UEFI Secure Boot、2 CPU以上、4 GB以上、virtual disk 1台へ
   固定します。追加diskは接続しません。
5. vTPM用暗号化は、専用の明示的なpasswordを設定します。自動生成された未知の
   passwordを使いません。`full`または`partial`の実値を後で固定します。
6. network adapterはCustomのhost-only `VMnet1`だけにします。NAT、bridged、2枚目の
   adapterは追加しません。
7. VMware Toolsをinstallせず、shared folder、clipboard、paste、drag and dropをすべて
   無効にします。VMX上でもcopy/paste/dndの3個の`isolation.tools.*.disable`、
   HGFSが有効化されていないこと、shared folder entryが0件であることを検査します。
8. ローカル管理者`vmtest`を作成し、このVMだけで使う空でないpasswordを設定します。
9. 固定test ISOを接続し、起動時にも接続する設定にします。
10. ISO内の`00-Bootstrap-VM-Lab.cmd`を右クリックし、管理者として実行します。
11. bootstrap成功後にWindowsを通常shutdownします。
12. Registry64のx64 Runtimeが未導入で、canonical 7名（`concrt140.dll`、`msvcp140.dll`、
    `msvcp140_1.dll`、`msvcp140_2.dll`、`vcomp140.dll`、`vcruntime140.dll`、
    `vcruntime140_1.dll`）、hashed名、その他のpattern一致名がないことを確認します。
    ただし正確な3名（`msvcp140_clr0400.dll`、`vcruntime140_clr0400.dll`、
    `vcruntime140_1_clr0400.dll`）がすべて存在する場合は、各ファイルについて
    Microsoft署名、OriginalFilename、System32/`WinSxS\amd64_netfx4-*` hardlink、
    `sfc /verifyfile` exit 0を確認した証拠が揃った場合だけWindows/.NET componentとして許可します。
    欠損、追加、署名・hardlink・SFC不成立はfail closedです。
13. powered-off root snapshot `A0-runtime-absent`を1個だけ作成します。
14. configのidentity値を一度`capture`にして`Capture`を実行し、返された
    `replacement_values`をconfigへ固定します。その後、`Plan`を実行します。

bootstrapはguestを`192.168.20.10/24`、hostを`192.168.20.1`として設定し、default routeを
除去します。`ActiveStore`全体を監査し、変更可能な`PersistentStore`のWinRM service/TCP 5985
inbound ruleだけをName指定で無効化したうえで、firewallをhost address、guest address、
interface、Private profile、TCP 5985へ限定した1規則だけにします。
同時にbootstrapの固定hash copyを`ProgramData\LoLReplayToolVMLab`へ保存し、
`SYSTEM`・ServiceAccount・Highest・AtStartupの固定Scheduled Taskを登録します。起動時はEthernet0の
準備、固定IP、default route不存在、他physical NIC不存在をbounded retryで確認してから
Private profileへ戻します。taskの有効状態、action、principal、唯一のBootTrigger、script hash、
終了コード、実行時刻はmarkerおよび`Inspect`で検証します。task script pathはmarkerから転記せず、
登録済みactionの`-File`引数から逆算して固定ProgramData path・実file hash・markerと照合します。
規則metadataは変更前に全件検証します。cmdletの途中失敗ではmarkerを作らず停止し、すでに処理した
local規則だけが無効のまま残る場合があります。原因解消後は同じbootstrapを再実行でき、最終判定は
再度`ActiveStore`全体に対して行います。
local administratorをWinRMで使用するため`LocalAccountTokenFilterPolicy=1`をtest VM内だけで
設定します。このVMをhost-only以外へ接続してはいけません。

## Host credential

credentialは現在のWindows userだけが復号できるPowerShell CLIXMLとして、repository外へ保存します。
例では`F:\VMware\Secrets`を使用します。

```powershell
New-Item -ItemType Directory -Path F:\VMware\Secrets
Get-Credential -UserName vm-encryption |
  Export-Clixml -LiteralPath F:\VMware\Secrets\vc-runtime-lab-vm.xml
Get-Credential -UserName vmtest |
  Export-Clixml -LiteralPath F:\VMware\Secrets\vc-runtime-lab-guest.xml
```

最初のcredentialではusernameは使用せず、VM暗号化passwordだけを保存します。CLIXMLは現在の
Windows userのDPAPIで保護されます。ただし`vmrun -vp`の仕様上、実行中だけpasswordがVMware
processのcommand lineへ現れ、同じホスト上でprocess command lineを読める主体には観測され得ます。
このため、他用途と共用しない廃棄可能なtest VM専用値だけを使い、`Run`ごとに
`-ConfirmVmPasswordProcessExposure`を要求します。一般userのinternet-facing VMにはこの方式を
転用しません。

WinRMはHTTP transportですが、`Negotiate`認証を使い、guest側の`AllowUnencrypted`と`Basic`を
無効にします。通信可能範囲はinternet routeを持たないVMnet1上のhost/guest 2 addressとTCP 5985へ
限定します。HTTPS certificate管理を追加せずに済ませる代わりに、このhost-only firewall境界、
VMXの単一NIC、default route不存在を毎回fail-closedで検査します。

host側WinRMはwildcardを使わず、管理者PowerShellでguest addressだけに固定します。

```powershell
Set-Item -LiteralPath WSMan:\localhost\Client\TrustedHosts `
  -Value 192.168.20.10 -Force
```

## Local config

configもrepository外へ置きます。passwordそのものをJSONへ書いてはいけません。以下のhashは
対象ISOを作り直すたびに、その実物から更新します。

```json
{
  "schema_version": 3,
  "vmrun_path": "F:\\VMware\\App\\vmrun.exe",
  "vmx_path": "F:\\VMware\\VMs\\LoLReplayTool-VC-Runtime-Lab\\LoLReplayTool-VC-Runtime-Lab.vmx",
  "vm_encryption_credential_path": "F:\\VMware\\Secrets\\vc-runtime-lab-vm.xml",
  "expected_vm_uuid": "capture",
  "expected_vm_encryption_type": "capture",
  "expected_guest_mac": "capture",
  "vmx_file_sha256": "capture",
  "vm_definition_fingerprint_sha256": "capture",
  "snapshot_a": "A0-runtime-absent",
  "snapshot_uid": "capture",
  "snapshot_fingerprint_sha256": "capture",
  "vmware_network": "vmnet1",
  "vmware_dhcp_config_path": "C:\\ProgramData\\VMware\\vmnetdhcp.conf",
  "vmware_nat_config_path": "C:\\ProgramData\\VMware\\vmnetnat.conf",
  "host_address": "192.168.20.1",
  "guest_address": "192.168.20.10",
  "guest_credential_path": "F:\\VMware\\Secrets\\vc-runtime-lab-guest.xml",
  "payload_iso_path": "F:\\VMware\\Media\\lol-vc-runtime-test-pr134-managed.iso",
  "payload_iso_sha256": "<lowercase SHA256>",
  "payload_volume_label": "LOL_VC_PR134",
  "runtime_installer_relative_path": "vc_redist.x64.exe",
  "runtime_installer_sha256": "843068991daaa1f73ad9f6239bce4d0f6a07a51f18c37ea2a867e9beca71295c",
  "installer_relative_path": "installer/LoLReplayTool-Setup-0.5.2.exe",
  "installer_sha256": "<lowercase SHA256>",
  "minimum_runtime_version": "14.44.35211.0",
  "app_relative_path": "LoLReplayTool-external-build\\LoLReplayTool.exe",
  "app_sha256": "3f8ec9a46c9509ed07197a765424eee95ebce50673a2500dd590cfa729aab09d",
  "environment_b_script_relative_path": "02-test-environment-b.ps1",
  "environment_b_script_sha256": "0a19971d4ecb8417ac06229f1e0bf124130c29760c791978d3153bc691992d46",
  "payload_commit": "1d5f79209646edda33911470ed132a9d5f4d440c",
  "artifact_root": "F:\\VMware\\Evidence\\vc-runtime-pr134"
}
```

`capture`は`Capture`だけで許可されます。schema 3のinstaller path/hashを含め、`replacement_values`を転記していないconfigは、`Plan`、
`Doctor`、`Run`で拒否されます。VMX file全体のSHA256は取得時点の証拠として記録します。
実行gateでは、snapshot復元時にVMwareが更新する`encryption.data`、電源状態、作業用delta disk名だけを
除いた全VMX key/valueをsemantic fingerprintとして固定します。作業用delta diskは名前を固定せず、
parent CIDと`parentFileNameHint`を辿って固定A0 snapshot diskへ到達することを毎回検査します。
snapshot fingerprintはVMSD内のA0 metadata、VMSN、および参照VMDK chainのpath、size、SHA256を
固定します。

## 実行

最初にVMを変更しない3操作を実行します。`Capture`はA0作成後に1回実行し、その出力をconfigへ
固定してから`Plan`と`Doctor`へ進みます。

```powershell
.\scripts\windows_vm_lab.ps1 `
  -Action Capture `
  -ConfigPath F:\VMware\Lab\vc-runtime-pr134.json
```

```powershell
.\scripts\windows_vm_lab.ps1 `
  -Action Plan `
  -ConfigPath F:\VMware\Lab\vc-runtime-pr134.json

.\scripts\windows_vm_lab.ps1 `
  -Action Doctor `
  -ConfigPath F:\VMware\Lab\vc-runtime-pr134.json
```

`Doctor`の`ready_for_run`が`true`で、実VMを変更する承認を得た場合だけ次を実行します。

```powershell
.\scripts\windows_vm_lab.ps1 `
  -Action Run `
  -ConfigPath F:\VMware\Lab\vc-runtime-pr134.json `
  -ConfirmSnapshotRestore `
  -ConfirmRuntimeInstall `
  -ConfirmVmPasswordProcessExposure
```

結果は`artifact_root`配下のrunごとのdirectoryへ保存します。credentialの内容やpasswordは
evidenceへ出力しません。

## 停止条件

次の場合は自動fallbackせず停止します。

- VM暗号化passwordを`vmrun`で検証できない
- VMX UUID/MAC/encryption/vTPM/NIC/ISO/isolationまたはsnapshot UID/fingerprintが一致しない
- VMX、VMSD、VMSN、VMDK chain、payload ISOのpathにreparse pointがある
- VMware Tools、default route、既存Runtime、canonical/hashed/unknownなSystem32 DLLをEnvironment Aで検出する
- CLR0400の3ファイルについて、Microsoft署名・OriginalFilename・System32/WinSxS hardlink・SFC exit 0の全証拠を確認できない
- ISO、Runtime installer、app、Environment B scriptのhashが変わる
- Microsoft署名を確認できない
- Runtime installが0または3010以外で終了する
- 3010により再起動が必要になる
- Environment A installerがexit 7以外を返す、または状態を変更する
- Environment B installer/self-check/update/uninstallのいずれかが失敗する
- WinRM firewallの有効規則を固定scopeの1件へ限定できない
- WinRM、Environment B検査、packaged self-check、guest OS shutdown後のpowered-off確認が失敗する

Runのcleanupは確立済みWinRM sessionから`shutdown.exe /s /t 0`（強制なし）を要求し、
sessionを閉じてから最大60秒pollします。session未確立時は`vmrun stop`を呼ばず、VMを起動中の
まま`manual_shutdown_required`として証拠化して失敗します。WinRM transport切断時はrequest送信を
`unknown`として記録し、最終VM stateを別に記録します。VMware Toolsは前提にしません。

3010では勝手にguestを再起動せず、evidenceを保存して管理者判断へ戻します。

CLR0400の検査定義はMicrosoftのVC++ Redistributable DLL命名資料、`fsutil hardlink`、
`sfc /verifyfile`、PowerShell署名検査の仕様に基づきます。

- <https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170>
- <https://learn.microsoft.com/en-us/cpp/windows/determining-which-dlls-to-redistribute?view=msvc-170>
- <https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/fsutil-hardlink>
- <https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/sfc>
- <https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.security/get-authenticodesignature>
