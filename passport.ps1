<#
.SYNOPSIS
    Collects computer passport information and saves to passport.json
.DESCRIPTION
    Gathers computer, OS, CPU, RAM, Disk, Top Processes, Hotfix,
    Critical Events, and Listening Ports count data.
#>

$Passport = [ordered]@{
    computer           = $env:COMPUTERNAME
    os                 = $null
    os_version         = $null
    cpu_name           = $null
    cpu_cores          = $null
    ram_gb             = $null
    disks              = @()
    top_processes      = @()
    hotfix_count       = 0
    critical_events_7d = 0
    listening_ports    = 0
    collected_at       = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
	latest_hotfixes      = @()   
    running_services     = 0     
    critical_events_list = @()   # TimeCreated, Id, ProviderName (max 20)
    video_driver         = $null # DeviceName, DriverVersion, DriverDate
    bios                 = $null # SMBIOSBIOSVersion, ReleaseDate
    net_adapters         = @()   # Name, Status, LinkSpeed
    listening_ports_detail = @() # port + PID (netstat equivalent)
    reference_file_hash  = $null # SHA256 of reference file
}

# --- Operating System ---
try {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    $Passport.os = $os.Caption
    $Passport.os_version = "$($os.Version) (Build $($os.BuildNumber))"
} catch {
    Write-Warning "Could not retrieve OS information: $_"
}

# --- CPU ---
try {
    $cpu = Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop | Select-Object -First 1
    $Passport.cpu_name = $cpu.Name
    $Passport.cpu_cores = $cpu.NumberOfCores
} catch {
    Write-Warning "Could not retrieve CPU information: $_"
}

# --- RAM (GB) ---
try {
    $ram = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
    $Passport.ram_gb = [math]::Round($ram.TotalPhysicalMemory / 1GB, 1)
} catch {
    try {
        $ramSum = Get-CimInstance -ClassName Win32_PhysicalMemory -ErrorAction Stop | Measure-Object -Property Capacity -Sum
        $Passport.ram_gb = [math]::Round($ramSum.Sum / 1GB, 1)
    } catch {
        Write-Warning "Could not retrieve RAM information: $_"
    }
}

# --- Disks (Letter / Size_GB / Free_GB) ---
try {
    $volumes = Get-Volume -ErrorAction Stop | Where-Object { $_.DriveType -eq 'Fixed' -and $_.DriveLetter }
    foreach ($vol in $volumes) {
        $diskInfo = [ordered]@{
            letter  = "$($vol.DriveLetter):"
            size_gb = [math]::Round($vol.Size / 1GB, 2)
            free_gb = [math]::Round($vol.SizeRemaining / 1GB, 2)
        }
        $Passport.disks += $diskInfo
    }
} catch {
    Write-Warning "Could not retrieve disk information: $_"
}

# --- Top 5 Processes (by Working Set / Memory) ---
try {
    $topProcs = Get-Process -ErrorAction Stop |
        Sort-Object WorkingSet -Descending |
        Select-Object -First 5
    foreach ($proc in $topProcs) {
        $Passport.top_processes += [ordered]@{
            name = $proc.Name
            mb   = [math]::Round($proc.WorkingSet / 1MB, 2)
        }
    }
    Write-Host "`n--- Top 5 processes (Name, Id, WorkingSet) ---"
    $topProcs | Select-Object Name, Id, WorkingSet | Format-Table -AutoSize | Out-String | Write-Host
} catch {
    Write-Warning "Could not retrieve top processes: $_"
}

# --- Hotfix Count ---
try {
    $hotfixes = Get-HotFix -ErrorAction Stop
    $Passport.hotfix_count = ($hotfixes | Measure-Object).Count

    $latest = $hotfixes |
        Sort-Object InstalledOn -Descending |
        Select-Object -First 5
    foreach ($hf in $latest) {
        $installed = if ($hf.InstalledOn) {
            $hf.InstalledOn.ToString("yyyy-MM-dd")
        } else { $null }
        $Passport.latest_hotfixes += [ordered]@{
            hotfix_id    = $hf.HotFixID
            description  = $hf.Description
            installed_on = $installed
        }
    }
} catch {
    Write-Warning "Could not retrieve hotfix information: $_"
}

# --- Critical Events (Last 7 Days) ---
try {
    $startTime = (Get-Date).AddDays(-7)
    $events = Get-WinEvent -FilterHashtable @{
        LogName   = 'System'
        Level     = @(1,2)   # Critical + Error
        StartTime = $startTime
    } -MaxEvents 20 -ErrorAction SilentlyContinue

    if ($events) {
        $Passport.critical_events_7d = ($events | Measure-Object).Count
        foreach ($ev in $events) {
            $Passport.critical_events_list += [ordered]@{
                time_created  = $ev.TimeCreated.ToString("yyyy-MM-dd HH:mm:ss")
                id            = $ev.Id
                provider_name = $ev.ProviderName
            }
        }
    } else {
        # No events is a valid outcome, not an error
        $Passport.critical_events_7d = 0
        Write-Host "No critical/error events found in System log for the last 7 days." -ForegroundColor Cyan
    }
} catch {
    $Passport.critical_events_7d = 0
    Write-Warning "Could not retrieve critical events: $_"
}

# --- Running Services Count ---
try {
    $running = Get-Service -ErrorAction Stop | Where-Object { $_.Status -eq 'Running' }
    $Passport.running_services = ($running | Measure-Object).Count
} catch {
    Write-Warning "Could not count running services: $_"
}

# --- Count of listening Ports ---
try {
    $listening = Get-NetTCPConnection -State Listen -ErrorAction Stop
    $Passport.listening_ports = ($listening | Measure-Object).Count
} catch {
    Write-Warning "Could not retrieve listening ports count: $_"
}

# --- Network Adapters (Name, Status, LinkSpeed) ---

try {
    $listeningDetailed = Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Select-Object LocalAddress, LocalPort, OwningProcess
    foreach ($conn in $listeningDetailed) {
        $Passport.listening_ports_detail += [ordered]@{
            port    = $conn.LocalPort
            address = $conn.LocalAddress
            pid     = $conn.OwningProcess
        }
    }
    $Passport.listening_ports_detail = $Passport.listening_ports_detail | Sort-Object port
} catch {
    Write-Warning "Could not retrieve detailed listening ports: $_"
}

# --- Video Driver (DeviceName, DriverVersion, DriverDate) ---

try {
    $display = Get-CimInstance Win32_PnPSignedDriver -ErrorAction Stop |
        Where-Object { $_.DeviceClass -eq 'DISPLAY' } |
        Select-Object -First 1
    if ($display) {
        $driverDate = if ($display.DriverDate) {
            ([datetime]$display.DriverDate).ToString("yyyy-MM-dd")
        } else { $null }
        $Passport.video_driver = [ordered]@{
            device_name    = $display.DeviceName
            driver_version = $display.DriverVersion
            driver_date    = $driverDate
        }
    }
} catch {
    Write-Warning "Could not retrieve video driver information: $_"
}

# --- BIOS (SMBIOSBIOSVersion, ReleaseDate)  [#6]

try {
    $bios = Get-CimInstance Win32_BIOS -ErrorAction Stop
    $release = if ($bios.ReleaseDate) {
        ([datetime]$bios.ReleaseDate).ToString("yyyy-MM-dd")
    } else { $null }
    $Passport.bios = [ordered]@{
        smbios_bios_version = $bios.SMBIOSBIOSVersion
        release_date        = $release
    }
} catch {
    Write-Warning "Could not retrieve BIOS information: $_"
}

# --- Reference File SHA256 ---

$refFile = "C:\UP04_Test\data\pf.txt"
try {
    if (Test-Path -LiteralPath $refFile) {
        $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $refFile -ErrorAction Stop
        $Passport.reference_file_hash = [ordered]@{
            path   = $refFile
            sha256 = $hash.Hash
        }
    } else {
        Write-Warning "Reference file not found: $refFile"
    }
} catch {
    Write-Warning "Could not compute reference file hash: $_"
}

# --- Convert to JSON and Save (UTF-8, explicit depth) ---

try {
    $json = $Passport | ConvertTo-Json -Depth 10
    $outPath = Join-Path -Path $PWD -ChildPath "passport.json"

    # Prefer UTF-8 without BOM (PS 7+); fall back to UTF8 for Windows PowerShell 5.1
    try {
        $json | Set-Content -Path $outPath -Encoding utf8NoBOM -ErrorAction Stop
    } catch {
        $json | Set-Content -Path $outPath -Encoding UTF8
    }
    Write-Host "`nPassport saved successfully to: $outPath" -ForegroundColor Green
} catch {
    Write-Error "Failed to save passport.json: $_"
}