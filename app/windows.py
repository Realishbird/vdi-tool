"""Unattended Windows 11 golden template build.

The tool writes a small ISO holding autounattend.xml and a first-logon script,
attaches it next to the Windows and VirtIO ISOs, and lets Windows Setup run
hands-free. The first logon installs the VirtIO guest tools (drivers and guest
agent), enables Remote Desktop, reports back and shuts down.
"""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .pve import PVE, PVEError

# Microsoft's public generic volume licence key for Windows 11 Pro. It selects the
# edition and skips the key prompt; it does not activate Windows.
GENERIC_PRO_KEY = "W269N-WFGWX-YVC9B-4J6C9-T83GX"
ARCH = 'processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"'


def _driver_paths() -> str:
    # The VirtIO CD's drive letter is not known in advance, so offer every likely one.
    out, n = [], 0
    for letter in "DEFGH":
        for drv in ("vioscsi", "NetKVM", "viostor"):
            n += 1
            out.append(f'<PathAndCredentials wcm:action="add" wcm:keyValue="{n}"><Path>{letter}:\\{drv}\\w11\\amd64</Path></PathAndCredentials>')
    return "".join(out)


def unattend_xml(computer: str, password: str) -> str:
    pw = escape(password)
    drivers = _driver_paths()
    first_logon = ("cmd /c for %d in (D E F G H I) do if exist %d:\\vdi-setup.ps1 "
                   "powershell -NoProfile -ExecutionPolicy Bypass -File %d:\\vdi-setup.ps1")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" {ARCH}>
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-PnpCustomizationsWinPE" {ARCH}>
      <DriverPaths>{drivers}</DriverPaths>
    </component>
    <component name="Microsoft-Windows-Setup" {ARCH}>
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>300</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall><OSImage>
        <InstallFrom><MetaData wcm:action="add"><Key>/IMAGE/NAME</Key><Value>Windows 11 Pro</Value></MetaData></InstallFrom>
        <InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo>
      </OSImage></ImageInstall>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <ProductKey><Key>{GENERIC_PRO_KEY}</Key><WillShowUI>Never</WillShowUI></ProductKey>
      </UserData>
    </component>
  </settings>
  <settings pass="offlineServicing">
    <component name="Microsoft-Windows-PnpCustomizationsNonWinPE" {ARCH}>
      <DriverPaths>{drivers}</DriverPaths>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" {ARCH}>
      <ComputerName>{escape(computer)}</ComputerName>
      <TimeZone>UTC</TimeZone>
    </component>
    <component name="Microsoft-Windows-Deployment" {ARCH}>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order>
          <Path>reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\OOBE /v BypassNRO /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" {ARCH}>
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" {ARCH}>
      <OOBE>
        <HideEULAPage>true</HideEULAPage><HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts><LocalAccounts>
        <LocalAccount wcm:action="add">
          <Name>vdi</Name><Group>Administrators</Group><DisplayName>VDI User</DisplayName>
          <Password><Value>{pw}</Value><PlainText>true</PlainText></Password>
        </LocalAccount>
      </LocalAccounts></UserAccounts>
      <AutoLogon>
        <Enabled>true</Enabled><Username>vdi</Username><LogonCount>1</LogonCount>
        <Password><Value>{pw}</Value><PlainText>true</PlainText></Password>
      </AutoLogon>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add"><Order>1</Order><CommandLine>{escape(first_logon)}</CommandLine></SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
"""


def _euc_ps1(base: str, password: str) -> str:
    """Install the EUC Score base package for all users and keep the vdi user logged on.

    Simloads drive real applications on screen, so they only run inside a logged-on desktop
    session. The automatic logon keeps the password in the registry, which is fine for test
    desktops and nothing else.
    """
    pw = password.replace("'", "''")
    return f"""# EUC Score base package, silent and for all users.
try {{
  Invoke-WebRequest -UseBasicParsing -Uri '{base}/agent/eucscore.exe' -OutFile C:\\Install-EUCScoreBase.exe -TimeoutSec 600
  Start-Process C:\\Install-EUCScoreBase.exe -ArgumentList '/VERYSILENT', '/ALLUSERS', '/SUPRESSMESSAGEBOXES', '/SP-', '/DIR=C:\\EUCScore' -Wait
  Remove-Item C:\\Install-EUCScoreBase.exe -Force
  icacls C:\\EUCScore\\Results /grant 'Users:(OI)(CI)M' | Out-Null
}} catch {{ Write-Output ('EUC Score install failed: ' + $_.Exception.Message) }}
$wl = 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon'
Set-ItemProperty $wl -Name AutoAdminLogon -Value '1'
Set-ItemProperty $wl -Name DefaultUserName -Value 'vdi'
Set-ItemProperty $wl -Name DefaultPassword -Value '{pw}'
Remove-ItemProperty $wl -Name AutoLogonCount -ErrorAction SilentlyContinue
"""


def setup_ps1(done_url: str, euc_base: str | None = None, password: str = "") -> str:
    euc = _euc_ps1(euc_base, password) if euc_base else ""
    return f"""$ErrorActionPreference = 'Continue'
Start-Transcript -Path C:\\vdi-setup.log
# VirtIO guest tools: remaining drivers plus the QEMU guest agent.
$tools = Get-PSDrive -PSProvider FileSystem | ForEach-Object {{ Join-Path $_.Root 'virtio-win-guest-tools.exe' }} | Where-Object {{ Test-Path $_ }} | Select-Object -First 1
if ($tools) {{ Start-Process $tools -ArgumentList '/install', '/passive', '/norestart' -Wait }}
# Remote Desktop on, sleep off.
Set-ItemProperty 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' -Name fDenyTSConnections -Value 0
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /hibernate off
{euc}# Tell vdi-tool the build is finished, then power off so it can become a template.
for ($i = 0; $i -lt 60; $i++) {{
  try {{ Invoke-WebRequest -UseBasicParsing -Method Post -Uri '{done_url}' -TimeoutSec 10 | Out-Null; break }} catch {{ Start-Sleep 5 }}
}}
Stop-Transcript
shutdown /s /t 10 /f
"""


def make_iso(xml: str, ps1: str) -> Path:
    """Write the answer files to a small ISO; the caller removes it after uploading."""
    work = Path(tempfile.mkdtemp(prefix="vdi-unattend-"))
    src = work / "src"
    src.mkdir()
    (src / "autounattend.xml").write_text(xml, encoding="utf-8")
    (src / "vdi-setup.ps1").write_text(ps1, encoding="utf-8")
    iso = work / "unattend.iso"
    try:
        subprocess.run(["genisoimage", "-quiet", "-J", "-r", "-V", "VDIUNATTEND", "-o", str(iso), str(src)],
                       check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise PVEError("genisoimage is not installed in the tool container; run the update again") from e
    except subprocess.CalledProcessError as e:
        raise PVEError(f"could not build the answer-file ISO: {e.stderr[-200:]}") from e
    return iso


async def press_any_key(pve: PVE, node: str, vmid: int, seconds: int = 25) -> None:
    """The Windows ISO waits for a key press before booting from CD; send Enter for a while."""
    for _ in range(seconds):
        try:
            await pve.put(f"/nodes/{node}/qemu/{vmid}/sendkey", key="ret")
        except PVEError:
            pass
        await asyncio.sleep(1)
