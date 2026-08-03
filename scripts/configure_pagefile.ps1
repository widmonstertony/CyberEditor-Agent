[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z]:$')]
    [string]$DataDrive = 'D:',

    [ValidateRange(16384, 131072)]
    [int]$InitialSizeMB = 32768,

    [ValidateRange(16384, 131072)]
    [int]$MaximumSizeMB = 49152,

    [ValidateRange(1024, 16384)]
    [int]$SystemDriveInitialMB = 4096,

    [ValidateRange(1024, 32768)]
    [int]$SystemDriveMaximumMB = 8192,

    [string]$ResultPath = ''
)

<#
.SYNOPSIS
Configure a bounded Windows page file for large local-model inference.

.DESCRIPTION
The script keeps a small page file on the Windows system drive and adds a
larger page file on the selected SSD. It never reboots Windows. The new sizes
normally become active after the next manual reboot.

#>

$ErrorActionPreference = 'Stop'

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )) {
        throw 'Administrator privileges are required.'
    }
}

function Set-BoundedPageFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [int]$InitialMB,
        [Parameter(Mandatory = $true)]
        [int]$MaximumMB
    )

    if ($MaximumMB -lt $InitialMB) {
        throw "Maximum page-file size must be >= initial size: $Name"
    }
    $existing = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ieq $Name }
    $properties = @{
        InitialSize = [uint32]$InitialMB
        MaximumSize = [uint32]$MaximumMB
    }
    if ($null -eq $existing) {
        New-CimInstance -ClassName Win32_PageFileSetting -Property (
            $properties + @{ Name = $Name }
        ) | Out-Null
    }
    else {
        Set-CimInstance -InputObject $existing -Property $properties | Out-Null
    }
}

Assert-Administrator

$volumeFilter = "DeviceID=`"{0}`"" -f $DataDrive
$volume = Get-CimInstance Win32_LogicalDisk -Filter $volumeFilter
if ($null -eq $volume -or $volume.DriveType -notin @(2, 3)) {
    throw "Page-file drive is unavailable: $DataDrive"
}
$requiredBytes = [int64]$InitialSizeMB * 1MB
if ([int64]$volume.FreeSpace -lt ($requiredBytes + 20GB)) {
    throw "Insufficient free space on ${DataDrive}; keep at least 20GB free after allocating the initial page file."
}

$computer = Get-CimInstance Win32_ComputerSystem
if ($computer.AutomaticManagedPagefile) {
    Set-CimInstance -InputObject $computer -Property @{
        AutomaticManagedPagefile = $false
    } | Out-Null
}

$systemPageFile = 'C:\pagefile.sys'
$dataPageFile = Join-Path $DataDrive 'pagefile.sys'
Set-BoundedPageFile -Name $systemPageFile -InitialMB $SystemDriveInitialMB -MaximumMB $SystemDriveMaximumMB
Set-BoundedPageFile -Name $dataPageFile -InitialMB $InitialSizeMB -MaximumMB $MaximumSizeMB

$result = [ordered]@{
    configured = $true
    reboot_required = $true
    automatic_managed = $false
    page_files = @(
        [ordered]@{
            name = $systemPageFile
            initial_mb = $SystemDriveInitialMB
            maximum_mb = $SystemDriveMaximumMB
        },
        [ordered]@{
            name = $dataPageFile
            initial_mb = $InitialSizeMB
            maximum_mb = $MaximumSizeMB
        }
    )
}
$json = $result | ConvertTo-Json -Depth 4
if ($ResultPath) {
    $resultFile = [System.IO.Path]::GetFullPath($ResultPath)
    $resultDirectory = Split-Path -Parent $resultFile
    if ($resultDirectory) {
        New-Item -ItemType Directory -Path $resultDirectory -Force | Out-Null
    }
    [System.IO.File]::WriteAllText(
        $resultFile,
        $json,
        [System.Text.UTF8Encoding]::new($false)
    )
}
$json
