# Runs install.ps1's tests as a throwaway non-admin Windows user (#150, PR C; Lachlan's choice
# (b), 2026-11-26). GitHub's Windows runners are elevated, so install.ps1 stops at its
# administrator refusal there; as this user, it reaches the folder, team-file and setup checks
# (must-refuse 12 to 14).
#
# The user's password is random for each run and never printed. The user, its scheduled task
# and its profile are deleted at the end, even when the tests fail.

$ErrorActionPreference = 'Stop'
$work = $env:GITHUB_WORKSPACE
$python = Join-Path $work '.venv\Scripts\python.exe'
$out = Join-Path $env:RUNNER_TEMP 'non-admin-tests'
$name = 'ddci' + [Guid]::NewGuid().ToString('N').Substring(0, 8)

$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
# Base64, plus one of each kind Windows asks for in a password.
$plain = [Convert]::ToBase64String($bytes) + 'a1!Z'
$secure = ConvertTo-SecureString $plain -AsPlainText -Force

$code = 1
try {
    New-LocalUser -Name $name -Password $secure -PasswordNeverExpires -UserMayNotChangePassword `
        -Description 'diffdata CI, deleted at the end of the job' | Out-Null
    # "Log on as a batch job", which a scheduled task needs. This group has it on Windows
    # Server and is not an administrator group.
    Add-LocalGroupMember -Group 'Performance Log Users' -Member $name

    New-Item -ItemType Directory -Force -Path $out | Out-Null
    & icacls $out /grant "${name}:(OI)(CI)M" /Q | Out-Null
    & icacls $work /grant "${name}:(OI)(CI)RX" /Q | Out-Null

    $runner = Join-Path $out 'run.cmd'
    $lines = @(
        '@echo off',
        "cd /d `"$work`"",
        'set DIFFDATA_CI_NONADMIN=1',
        "`"$python`" -m pytest tests/test_install.py -k ps1 -p no:cacheprovider -rs -q > `"$out\log.txt`" 2>&1",
        "echo %ERRORLEVEL% > `"$out\code.txt`""
    )
    Set-Content -LiteralPath $runner -Value ($lines -join "`r`n") -Encoding ASCII

    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$runner`""
    Register-ScheduledTask -TaskName $name -Action $action -User $name -Password $plain `
        -RunLevel Limited | Out-Null
    Start-ScheduledTask -TaskName $name

    $deadline = (Get-Date).AddMinutes(15)
    while (-not (Test-Path (Join-Path $out 'code.txt')) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
    }
    if (Test-Path (Join-Path $out 'log.txt')) { Get-Content (Join-Path $out 'log.txt') }
    if (Test-Path (Join-Path $out 'code.txt')) {
        $code = [int](Get-Content (Join-Path $out 'code.txt') -Raw).Trim()
    }
    else {
        Write-Host 'The tests as the non-admin user did not finish within 15 minutes.'
        $info = Get-ScheduledTaskInfo -TaskName $name
        Write-Host "Task result: $($info.LastTaskResult)"
    }
}
finally {
    $plain = $null
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_UserProfile |
        Where-Object { $_.LocalPath -like "*\$name" } |
        Remove-CimInstance -ErrorAction SilentlyContinue
    Remove-LocalUser -Name $name -ErrorAction SilentlyContinue
    if (Get-LocalUser -Name $name -ErrorAction SilentlyContinue) {
        Write-Host "The throwaway user $name could not be deleted."
        $code = 1
    }
}
exit $code
