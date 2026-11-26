# diffdata installer for Windows (#150, item 8). Run in your own PowerShell:
#
#   irm https://raw.githubusercontent.com/SPARDiffData/differential-data-collection/main/install.ps1 | iex
#
# or, to hand setup your team file straight away:
#
#   $env:DIFFDATA_TEAM_FILE = "$HOME\Downloads\DD-7KQ4.diffdata-team.json"; irm https://raw.githubusercontent.com/SPARDiffData/differential-data-collection/main/install.ps1 | iex
#
# It checks the folder first, then for Git (and installs it with winget if it can), installs
# uv from Astral if it's missing, clones this repo to %USERPROFILE%\differential-data-collection
# (or $env:DIFFDATA_REPO) or fast-forwards a clean clone, runs `uv sync`, then hands over to
# `diffdata setup`.
#
# What it refuses (#150's must-refuse list, approved on 2026-11-26):
#   11  to run as administrator (an elevated window)
#   12  to change a folder that isn't a clean clone of this repo on main (no changes, no
#       untracked files, its own work tree), or one inside ~\.diffdata: it stops with nothing
#       changed, before installing anything; an update only fast-forwards
#   13  to read, print, copy or move the team file, or touch ~\.diffdata: it passes the path
#       to setup only
#   14  to go on after a failed step, or to hand over to setup without a window a person types
#       in, or under Claude Code (CLAUDECODE set): it prints the setup line instead
#   15  to edit profiles itself, or to fetch the repo or uv from anywhere but GitHub (this repo,
#       with your own Git settings' URL rewrites ignored), Astral (uv's installer, with its
#       download overrides cleared) and winget (Git). uv's installer adds uv to your PATH, as
#       it always does. `uv sync` then installs the locked Python packages from PyPI.
#
# Everything runs inside one script block, and stops with `return`, never by ending the
# session: under `irm | iex`, that would close the person's PowerShell window.

& {
    $ErrorActionPreference = 'Stop'
    $RepoUrl = 'https://github.com/SPARDiffData/differential-data-collection.git'
    $Dest = if ($env:DIFFDATA_REPO) { $env:DIFFDATA_REPO } else { Join-Path $env:USERPROFILE 'differential-data-collection' }
    # Relative paths are taken from PowerShell's current folder ($PWD).
    function Full([string]$Path) { $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path) }
    $Dest = [IO.Path]::GetFullPath((Full $Dest))
    if ($Dest.Length -gt 3) { $Dest = $Dest.TrimEnd('\', '/') }  # never C:\ into C:
    $TeamFile = $env:DIFFDATA_TEAM_FILE
    if ($TeamFile) { $TeamFile = Full $TeamFile }  # a path only; never opened
    foreach ($name in 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
        'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS',
        'UV_PROJECT_ENVIRONMENT', 'UV_PROJECT', 'UV_WORKING_DIR') {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }

    function Say([string]$Text) { Write-Host $Text }
    function Fail([string]$Text) { Write-Host "diffdata install: $Text" -ForegroundColor Red }
    function Refresh-Path {
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    }
    function Run([string]$What, [scriptblock]$Command) {
        & $Command
        if ($LASTEXITCODE -ne 0) { throw "$What failed (code $LASTEXITCODE)." }
    }
    function Quote([string]$Text) {
        # PowerShell also ends a quote at curly apostrophes, so each is doubled too.
        foreach ($q in "'", [char]0x2018, [char]0x2019, [char]0x201A, [char]0x201B) { $Text = $Text.Replace([string]$q, "$q$q") }
        "'" + $Text + "'"
    }
    # Git with your own and the system's settings (URL rewrites, hooks) left out, for every
    # check and fetch, so the remote that's checked is the one that's fetched.
    $emptyConfig = [IO.Path]::GetTempFileName()
    function IsoGit {
        $saved = $env:GIT_CONFIG_GLOBAL, $env:GIT_CONFIG_NOSYSTEM
        $env:GIT_CONFIG_GLOBAL = $emptyConfig; $env:GIT_CONFIG_NOSYSTEM = '1'
        try { & git @args } finally { $env:GIT_CONFIG_GLOBAL, $env:GIT_CONFIG_NOSYSTEM = $saved }
    }

    try {
        # 11: never elevated.
        $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            Fail "Don't run this as administrator: open PowerShell normally and run the install line again. Nothing was changed."
            return
        }

        # 12, first part: the folder, before anything is installed.
        $settings = [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.diffdata')).TrimEnd('\') + '\'
        if (($Dest.TrimEnd('\') + '\').StartsWith($settings, [StringComparison]::OrdinalIgnoreCase)) {
            Fail "$Dest is inside ~\.diffdata, which holds your settings and key. Choose another folder. Nothing was changed."
            return
        }
        # A linked folder (junction or link) above it could send the clone somewhere else.
        $above = Split-Path -Parent $Dest
        while ($above) {
            if (Test-Path -LiteralPath $above) {
                if ((Get-Item -LiteralPath $above -Force).LinkType) {
                    Fail "$above is a link, so $Dest could end up somewhere else. Choose another folder. Nothing was changed."
                    return
                }
            }
            $above = Split-Path -Parent $above
        }
        $update = $false
        if (Test-Path -LiteralPath $Dest) {
            $item = Get-Item -LiteralPath $Dest -Force
            if ($item.LinkType -or -not $item.PSIsContainer -or -not (Test-Path -LiteralPath (Join-Path $Dest '.git'))) {
                Fail "$Dest is there, but isn't a clone of diffdata. Move it away, or set DIFFDATA_REPO to another folder. Nothing was changed."
                return
            }
            $update = $true
        }

        # Git.
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            if ($update) {
                # A clone is checked before anything is installed: install Git yourself first.
                Fail "Git is missing, so $Dest can't be checked. Install it from https://git-scm.com/download/win, then run the install line again. Nothing was changed."
                return
            }
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                Say 'Installing Git with winget (it may ask you to allow it)...'
                Run 'Installing Git' { winget install --id Git.Git -e --source winget }
                Refresh-Path
            }
            if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
                Fail 'Git is missing. Install it from https://git-scm.com/download/win, then open a new PowerShell and run the install line again.'
                return
            }
        }

        # 12, second part: only a clean clone of this repo, on main, in its own work tree.
        if ($update) {
            $top = (IsoGit --no-optional-locks -C $Dest rev-parse --show-toplevel 2>$null)
            $ok = $LASTEXITCODE -eq 0 -and $top -and ([IO.Path]::GetFullPath($top) -eq $Dest)
            $gitDir = (IsoGit --no-optional-locks -C $Dest rev-parse --absolute-git-dir 2>$null)
            $ok = $ok -and $LASTEXITCODE -eq 0 -and ([IO.Path]::GetFullPath($gitDir) -eq (Join-Path $Dest '.git'))
            $common = (IsoGit --no-optional-locks -C $Dest rev-parse --path-format=absolute --git-common-dir 2>$null)
            $ok = $ok -and $LASTEXITCODE -eq 0 -and ([IO.Path]::GetFullPath($common) -eq (Join-Path $Dest '.git'))
            if (-not $ok) {
                Fail "$Dest's Git settings point to another folder, or it isn't a working clone. Nothing was changed."
                return
            }
            $remote = (IsoGit --no-optional-locks -C $Dest remote get-url origin 2>$null)
            if ($LASTEXITCODE -ne 0 -or ($remote -cne 'https://github.com/SPARDiffData/differential-data-collection' -and $remote -cne $RepoUrl)) {
                Fail "$Dest is a clone of another repository. Move it away, or set DIFFDATA_REPO to another folder. Nothing was changed."
                return
            }
            $branch = (IsoGit --no-optional-locks -C $Dest rev-parse --abbrev-ref HEAD 2>$null)
            if ($LASTEXITCODE -ne 0 -or $branch -cne 'main') {
                Fail "$Dest is on branch $branch, not main. Switch it to main yourself, then run the install line again. Nothing was changed."
                return
            }
            $changes = (IsoGit --no-optional-locks -C $Dest status --porcelain --untracked-files=normal)
            if ($LASTEXITCODE -ne 0) {
                Fail "Couldn't check $Dest for changes. Nothing was changed."
                return
            }
            if ($changes) {
                Fail "$Dest has local changes or files of yours. Keep or move them yourself, then run the install line again. Nothing was changed."
                return
            }
        }

        # uv, called by its full path, so a new PATH in this same window isn't needed.
        function Find-Uv {
            $found = Get-Command uv -ErrorAction SilentlyContinue
            if ($found) { return $found.Source }
            foreach ($candidate in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'), (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'))) {
                if (Test-Path $candidate -PathType Leaf) { return $candidate }
            }
            return $null
        }
        $Uv = Find-Uv
        if (-not $Uv) {
            Say 'Installing uv, from Astral (https://astral.sh/uv)...'
            foreach ($name in 'UV_DOWNLOAD_URL', 'UV_INSTALLER_GITHUB_BASE_URL', 'UV_INSTALLER_GHE_BASE_URL') {
                Remove-Item "Env:$name" -ErrorAction SilentlyContinue
            }
            Run 'Installing uv' { powershell -NoProfile -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex' }
            Refresh-Path
            $Uv = Find-Uv
            if (-not $Uv) {
                Fail 'uv was installed, but this window cannot find it. Open a new PowerShell and run the install line again.'
                return
            }
        }

        if ($update) {
            Say "Updating $Dest..."
            Run 'Fetching the repo' { IsoGit -C $Dest fetch origin main }
            Run 'Updating the repo' { IsoGit -C $Dest merge --ff-only --no-overwrite-ignore FETCH_HEAD }
        }
        else {
            Say "Getting diffdata into $Dest..."
            Run 'Getting the repo' { IsoGit clone --branch main $RepoUrl $Dest }
        }

        Push-Location -LiteralPath $Dest
        try {
            Run 'uv sync' { & $Uv sync --locked --project $Dest }

            if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
                Say 'Claude Code was not found as a command. /slack-export needs it (or the Claude desktop app): see https://claude.com/claude-code'
            }

            # 14: hand over to setup only in a window a person types in, never under Claude Code.
            $line = "Set-Location -LiteralPath $(Quote $Dest) -ErrorAction Stop; & $(Quote $Uv) run diffdata setup"
            if ($TeamFile) { $line = "$line $(Quote $TeamFile)" }
            $byPerson = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected
            if ((Test-Path Env:CLAUDECODE) -or -not $byPerson) {
                Say 'Installed. Now run setup yourself, in your own PowerShell:'
                Say "  $line"
                return
            }
            Say 'Installed. Starting setup...'
            if ($TeamFile) { & $Uv run diffdata setup $TeamFile } else { & $Uv run diffdata setup }
        }
        finally {
            Pop-Location
        }
    }
    catch {
        Fail "$($_.Exception.Message) Fix that, then run the install line again."
    }
    finally {
        Remove-Item -LiteralPath $emptyConfig -ErrorAction SilentlyContinue
    }
}
