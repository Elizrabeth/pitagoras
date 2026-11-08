; Pitagoras installer (Inno Setup 6.4+). Build it with installer\build.ps1.
; The installer itself is small: it copies Pitagoras, downloads the brain you pick (with a progress bar),
; then setup.ps1 puts a portable Python 3.12, the packages, the voice and llama.cpp into the same folder.
; Everything lives in the folder you choose: nothing goes into AppData or onto the system.

#define AppName "Pitagoras"
#ifndef Suffix
  #define Suffix ""
#endif
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef Publisher
  #define Publisher "Pitagoras"
#endif
#ifndef GitHubRepo
  #define GitHubRepo "Elizrabeth/pitagoras"
#endif
#ifndef RepoUrl
  #define RepoUrl "https://github.com/" + GitHubRepo
#endif

[Setup]
AppId={{6F1E4C1B-6A7E-4F2B-9C3B-2D7A9B1E5A40}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
; Pitagoras writes into its own folder (memory, settings, logs, models), so it installs per user, no admin
PrivilegesRequired=lowest
; C:\Pitagoras by default; the folder page has Browse… (its dialog has "Make New Folder")
DefaultDirName={sd}\Pitagoras
DefaultGroupName={#AppName}
DisableDirPage=no
AlwaysShowDirOnReadyPage=yes
DisableProgramGroupPage=yes
AllowNoIcons=yes
UsePreviousAppDir=yes
; an update closes a running Pitagoras first (Restart Manager)
CloseApplications=yes
LicenseFile=..\LICENSE
SetupIconFile=..\launcher\pitagoras.ico
UninstallDisplayIcon={app}\Pitagoras.exe
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\dist
OutputBaseFilename=Pitagoras-Setup-{#AppVersion}{#Suffix}
; Python + packages (~8 GB) are downloaded by setup.ps1 (the brain's size is counted by its [Files] entries)
ExtraDiskSpaceRequired=9000000000
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "pl"; MessagesFile: "compiler:Languages\Polish.isl"

[CustomMessages]
en.Downloads=Downloads (done while installing):
pl.Downloads=Pobieranie (podczas instalacji):
en.TaskBrain=Download a brain (the AI model, ~6 GB, can see the screen):
pl.TaskBrain=Pobierz mózg (model AI, ~6 GB, widzi ekran):
en.TaskBrainGoogle=Gemma 4 E4B by Google (official, 5.8 GB)
pl.TaskBrainGoogle=Gemma 4 E4B od Google (oficjalny, 5,8 GB)
en.TaskBrainUncensored=Gemma 4 E4B Uncensored by HauhauCS (fewer refusals, 5.9 GB)
pl.TaskBrainUncensored=Gemma 4 E4B Uncensored od HauhauCS (mniej odmów, 5,9 GB)
en.TaskStartMenu=Start menu shortcuts
pl.TaskStartMenu=Skróty w menu Start
en.TaskLlama=llama.cpp, to run local brains on an NVIDIA GPU (~0.5 GB)
pl.TaskLlama=llama.cpp do uruchamiania lokalnych mózgów na karcie NVIDIA (~0,5 GB)
en.TaskPiper=Polish offline voice (Piper)
pl.TaskPiper=Polski głos offline (Piper)
en.Shortcuts=Shortcuts:
pl.Shortcuts=Skróty:
en.SetupStatus=Setting up Python and downloading (several GB, this can take a while)...
pl.SetupStatus=Instalowanie Pythona i pobieranie (kilka GB, to może chwilę potrwać)...
en.ProtectedDir=Pitagoras saves its memory, settings and models in its own folder, so it can't go into Program Files.%n%nPick another folder (for example on a drive with plenty of space).
pl.ProtectedDir=Pitagoras zapisuje pamięć, ustawienia i modele we własnym folderze, więc nie może być w Program Files.%n%nWybierz inny folder (np. na dysku z dużą ilością miejsca).
en.DeleteData=Also delete your brains, voice models, chat memory, personality and settings?%n%n(No = keep them in %1 for a later reinstall.)
pl.DeleteData=Usunąć też mózgi, modele głosu, pamięć rozmów, osobowość i ustawienia?%n%n(Nie = zostaną w %1 na wypadek ponownej instalacji.)
en.NoBrainHint=No brain was downloaded. Pick one later from the character's right-click menu > Brain (LM Studio, Ollama, Claude, or a GGUF in the brains folder).
pl.NoBrainHint=Nie pobrano mózgu. Wybierz go później w menu postaci (prawy klik) > Brain (LM Studio, Ollama, Claude albo plik GGUF w folderze brains).
en.UpdateAvailable=Pitagoras %1 is installed, and version %2 is on GitHub.%n%nDownload and install %2 now? Your brains, voices, memory, personality and settings are kept.%n%n(No = carry on with this installer.)
pl.UpdateAvailable=Zainstalowany jest Pitagoras %1, a na GitHubie jest wersja %2.%n%nPobrać i zainstalować %2 teraz? Mózgi, głosy, pamięć, osobowość i ustawienia zostaną.%n%n(Nie = kontynuuj z tym instalatorem.)
en.UpdateAvailableCheck=Pitagoras %1 is installed, and version %2 is on GitHub.%n%nDownload and install it now? Your brains, voices, memory, personality and settings are kept.
pl.UpdateAvailableCheck=Zainstalowany jest Pitagoras %1, a na GitHubie jest wersja %2.%n%nPobrać i zainstalować ją teraz? Mózgi, głosy, pamięć, osobowość i ustawienia zostaną.
en.UpToDate=Pitagoras %1 is up to date (the newest version on GitHub is %2).
pl.UpToDate=Pitagoras %1 jest aktualny (najnowsza wersja na GitHubie to %2).
en.CheckFailed=Couldn't reach GitHub to check for updates. Check your internet connection and try again.
pl.CheckFailed=Nie udało się połączyć z GitHubem, żeby sprawdzić aktualizacje. Sprawdź internet i spróbuj ponownie.
en.UpdateFailed=Downloading the update failed:%n%1%n%nYou can download it yourself from {#RepoUrl}/releases
pl.UpdateFailed=Pobieranie aktualizacji nie powiodło się:%n%1%n%nMożesz pobrać ją ręcznie z {#RepoUrl}/releases
en.Downloading=Downloading Pitagoras %1...
pl.Downloading=Pobieranie Pitagorasa %1...
en.Downgrade=A newer Pitagoras (%1) is installed. Install the older %2 over it?
pl.Downgrade=Zainstalowany jest nowszy Pitagoras (%1). Zainstalować starszą wersję %2 zamiast niego?
en.UpdateCaption=Update Pitagoras %1 to %2
pl.UpdateCaption=Aktualizacja Pitagorasa %1 do %2
en.CheckUpdates=Check for {#AppName} updates
pl.CheckUpdates=Sprawdź aktualizacje {#AppName}

[Tasks]
Name: "brain"; Description: "{cm:TaskBrain}"; GroupDescription: "{cm:Downloads}"
Name: "brain\google"; Description: "{cm:TaskBrainGoogle}"; Flags: exclusive
Name: "brain\uncensored"; Description: "{cm:TaskBrainUncensored}"; Flags: exclusive unchecked
Name: "llama"; Description: "{cm:TaskLlama}"; GroupDescription: "{cm:Downloads}"
Name: "piper"; Description: "{cm:TaskPiper}"; GroupDescription: "{cm:Downloads}"; Flags: unchecked
Name: "startmenu"; Description: "{cm:TaskStartMenu}"; GroupDescription: "{cm:Shortcuts}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:Shortcuts}"

[Files]
Source: "..\Pitagoras.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\run.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\setup.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\pitagoras\*"; DestDir: "{app}\pitagoras"; Excludes: "__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\examples\*"; DestDir: "{app}\examples"; Excludes: "__pycache__"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "..\launcher\*"; DestDir: "{app}\launcher"; Flags: ignoreversion
; a copy of this installer: the "Check for updates" shortcut runs it with /CHECKUPDATE=1
Source: "{srcexe}"; DestDir: "{app}"; DestName: "Pitagoras-Setup.exe"; Flags: external ignoreversion; Check: NotRunFromApp
#ifdef Personality
; the default personality (only if there isn't one yet: a reinstall never overwrites yours)
Source: "{#Personality}"; DestDir: "{app}"; DestName: "personality.txt"; Flags: onlyifdoesntexist uninsneveruninstall
#endif

; the brain, downloaded straight into the install folder (the uninstaller asks before deleting it)
#define GoogleUrl "https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf/resolve/main/"
#define HauhauUrl "https://huggingface.co/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive/resolve/main/"
Source: "{#GoogleUrl}gemma-4-E4B_q4_0-it.gguf"; DestDir: "{app}\brains\Gemma-4-E4B"; DestName: "gemma-4-E4B_q4_0-it.gguf"; ExternalSize: 5154941280; Flags: external download ignoreversion uninsneveruninstall; Tasks: brain\google; Check: BrainMissing('gemma-4-E4B_q4_0-it.gguf')
Source: "{#GoogleUrl}gemma-4-E4B-it-mmproj.gguf"; DestDir: "{app}\brains\Gemma-4-E4B"; DestName: "gemma-4-E4B-it-mmproj.gguf"; ExternalSize: 991552256; Flags: external download ignoreversion uninsneveruninstall; Tasks: brain\google; Check: BrainMissing('gemma-4-E4B-it-mmproj.gguf')
Source: "{#HauhauUrl}Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"; DestDir: "{app}\brains\Gemma-4-E4B"; DestName: "Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"; ExternalSize: 5335285728; Flags: external download ignoreversion uninsneveruninstall; Tasks: brain\uncensored; Check: BrainMissing('Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf')
Source: "{#HauhauUrl}mmproj-Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-f16.gguf"; DestDir: "{app}\brains\Gemma-4-E4B"; DestName: "mmproj-Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-f16.gguf"; ExternalSize: 990288832; Flags: external download ignoreversion uninsneveruninstall; Tasks: brain\uncensored; Check: BrainMissing('mmproj-Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-f16.gguf')

[Dirs]
Name: "{app}\brains"
Name: "{app}\voices"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\Pitagoras.exe"; WorkingDir: "{app}"; Tasks: startmenu
Name: "{group}\{#AppName} folder"; Filename: "{app}"; Tasks: startmenu
Name: "{group}\Repair {#AppName} (re-run setup)"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup.ps1"" -FromInstaller"; WorkingDir: "{app}"; Tasks: startmenu
Name: "{group}\{cm:CheckUpdates}"; Filename: "{app}\Pitagoras-Setup.exe"; Parameters: "/CHECKUPDATE=1"; WorkingDir: "{app}"; Tasks: startmenu
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"; Tasks: startmenu
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Pitagoras.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup.ps1"" -FromInstaller {code:SetupArgs}"; WorkingDir: "{app}"; StatusMsg: "{cm:SetupStatus}"; Flags: waituntilterminated; Check: not SkipSetup
Filename: "{app}\Pitagoras.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; things setup.ps1 / Pitagoras made that can always be downloaded again
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\python"
Type: files; Name: "{app}\python.tar.gz*"
Type: files; Name: "{app}\overlay.ini"
Type: files; Name: "{app}\Pitagoras-Setup.exe"
Type: filesandordirs; Name: "{app}\bin"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\pitagoras"
Type: filesandordirs; Name: "{app}\launcher"
Type: filesandordirs; Name: "{app}\examples"

[Code]
// for testing the installer:  Pitagoras-Setup.exe /NOSETUP=1  (copies files only, downloads nothing)
//   /CHECKUPDATE=1   only check GitHub for a newer version (the Start menu shortcut)
//   /NOUPDATECHECK=1 don't ask GitHub at all
const
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F1E4C1B-6A7E-4F2B-9C3B-2D7A9B1E5A40}_is1';

var
  InstalledVer, InstalledDir: String;
  IsUpdate: Boolean;

function SkipSetup: Boolean;
begin
  Result := ExpandConstant('{param:NOSETUP|0}') = '1';
end;

function BrainMissing(Name: String): Boolean;
begin
  Result := not FileExists(ExpandConstant('{app}\brains\Gemma-4-E4B\') + Name);
end;

function NotRunFromApp: Boolean;
begin
  Result := CompareText(ExpandConstant('{srcexe}'), ExpandConstant('{app}\Pitagoras-Setup.exe')) <> 0;
end;

// "1.10.2" vs "1.9" -> 1 / 0 / -1
function NextNum(var S: String): Integer;
var
  P: Integer;
begin
  P := Pos('.', S);
  if P = 0 then
  begin
    Result := StrToIntDef(Trim(S), 0);
    S := '';
  end else
  begin
    Result := StrToIntDef(Trim(Copy(S, 1, P - 1)), 0);
    Delete(S, 1, P);
  end;
end;

function CompareVer(A, B: String): Integer;
var
  I, X, Y: Integer;
begin
  Result := 0;
  for I := 1 to 4 do
  begin
    X := NextNum(A);
    Y := NextNum(B);
    if X <> Y then
    begin
      if X > Y then Result := 1 else Result := -1;
      Exit;
    end;
  end;
end;

// the newest release's version on GitHub ("1.2.0"), or '' when it can't be reached
function LatestOnGitHub: String;
var
  Http: Variant;
  Body: String;
  P: Integer;
begin
  Result := '';
  try
    Http := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Http.SetTimeouts(5000, 5000, 8000, 8000);
    Http.Open('GET', 'https://api.github.com/repos/{#GitHubRepo}/releases/latest', False);
    Http.SetRequestHeader('User-Agent', 'Pitagoras-Setup');
    Http.SetRequestHeader('Accept', 'application/vnd.github+json');
    Http.Send('');
    if Http.Status <> 200 then Exit;
    Body := Http.ResponseText;
    P := Pos('"tag_name"', Body);
    if P = 0 then Exit;
    Delete(Body, 1, P + 9);
    P := Pos('"', Body);
    Delete(Body, 1, P);
    P := Pos('"', Body);
    Result := Trim(Copy(Body, 1, P - 1));
    if (Length(Result) > 0) and ((Result[1] = 'v') or (Result[1] = 'V')) then Delete(Result, 1, 1);
  except
    Result := '';
  end;
end;

// downloads the GitHub installer and runs it over the installed folder; True = it ran
function RunGitHubInstaller(Ver: String): Boolean;
var
  Name, Params: String;
  Code: Integer;
begin
  Result := False;
  Name := 'Pitagoras-Setup-' + Ver + '.exe';
  try
    DownloadTemporaryFile('{#RepoUrl}/releases/download/v' + Ver + '/' + Name, Name, '', nil);
    Params := '/NOUPDATECHECK=1';
    if InstalledDir <> '' then Params := Params + ' /DIR="' + RemoveBackslashUnlessRoot(InstalledDir) + '"';
    // wait for it: our temp folder (with its file) goes away when this setup exits
    Result := Exec(ExpandConstant('{tmp}\') + Name, Params, '', SW_SHOW, ewWaitUntilTerminated, Code);
    if not Result then RaiseException(SysErrorMessage(Code));
  except
    MsgBox(FmtMessage(CustomMessage('UpdateFailed'), [GetExceptionMessage]), mbError, MB_OK);
  end;
end;

function InitializeSetup: Boolean;
var
  Latest: String;
  CheckOnly, Newer: Boolean;
begin
  Result := True;
  CheckOnly := ExpandConstant('{param:CHECKUPDATE|0}') = '1';
  if not RegQueryStringValue(HKCU, UninstKey, 'DisplayVersion', InstalledVer) then
    RegQueryStringValue(HKLM, UninstKey, 'DisplayVersion', InstalledVer);
  if not RegQueryStringValue(HKCU, UninstKey, 'InstallLocation', InstalledDir) then
    RegQueryStringValue(HKLM, UninstKey, 'InstallLocation', InstalledDir);
  IsUpdate := InstalledVer <> '';

  // already installed: is there something newer than both it and this installer on GitHub?
  if (IsUpdate or CheckOnly) and not WizardSilent and (ExpandConstant('{param:NOUPDATECHECK|0}') <> '1') then
  begin
    Latest := LatestOnGitHub;
    if CheckOnly and (Latest = '') then
    begin
      MsgBox(CustomMessage('CheckFailed'), mbError, MB_OK);
      Result := False;
      Exit;
    end;
    Newer := (Latest <> '') and (CompareVer(Latest, InstalledVer) > 0) and (CompareVer(Latest, '{#AppVersion}') > 0);
    if Newer then
    begin
      if CheckOnly then
        Newer := MsgBox(FmtMessage(CustomMessage('UpdateAvailableCheck'), [InstalledVer, Latest]), mbConfirmation, MB_YESNO) = IDYES
      else
        Newer := MsgBox(FmtMessage(CustomMessage('UpdateAvailable'), [InstalledVer, Latest]), mbConfirmation, MB_YESNO) = IDYES;
      if Newer and RunGitHubInstaller(Latest) then
      begin
        Result := False;
        Exit;
      end;
    end else if CheckOnly then
      MsgBox(FmtMessage(CustomMessage('UpToDate'), [InstalledVer, Latest]), mbInformation, MB_OK);
    if CheckOnly then
    begin
      Result := False;
      Exit;
    end;
  end;

  if IsUpdate and not WizardSilent and (CompareVer(InstalledVer, '{#AppVersion}') > 0) then
    Result := MsgBox(FmtMessage(CustomMessage('Downgrade'), [InstalledVer, '{#AppVersion}']), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure InitializeWizard;
begin
  if IsUpdate then
    WizardForm.Caption := FmtMessage(CustomMessage('UpdateCaption'), [InstalledVer, '{#AppVersion}']);
end;

// an update goes into the same folder and has already been accepted: skip the license and folder pages
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := IsUpdate and ((PageID = wpLicense) or (PageID = wpSelectDir));
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  // the brain is already there: don't offer the 6 GB download again
  if IsUpdate and (CurPageID = wpSelectTasks) then
    WizardSelectTasks('!brain');
end;

function SetupArgs(Param: String): String;
begin
  Result := '';
  if WizardIsTaskSelected('llama') then Result := Result + ' -WithLlamaCpp';
  if WizardIsTaskSelected('piper') then Result := Result + ' -WithPiperPolish';
end;

function StartsWithDir(Dir, Prefix: String): Boolean;
begin
  Prefix := AddBackslash(Lowercase(Prefix));
  Result := (Prefix <> '\') and (Pos(Prefix, AddBackslash(Lowercase(Dir))) = 1);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Dir := WizardDirValue;
    if StartsWithDir(Dir, ExpandConstant('{commonpf64}')) or StartsWithDir(Dir, ExpandConstant('{commonpf32}'))
      or StartsWithDir(Dir, ExpandConstant('{win}')) then
    begin
      MsgBox(CustomMessage('ProtectedDir'), mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssDone) and not IsUpdate and not WizardIsTaskSelected('brain') and not WizardSilent then
    MsgBox(CustomMessage('NoBrainHint'), mbInformation, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  App: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    App := ExpandConstant('{app}');
    if UninstallSilent then Exit;
    if MsgBox(FmtMessage(CustomMessage('DeleteData'), [App]), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(App + '\brains', True, True, True);
      DelTree(App + '\voices', True, True, True);
      DelTree(App + '\memory', True, True, True);
      DeleteFile(App + '\settings.yaml');
      DeleteFile(App + '\personality.txt');
      DeleteFile(App + '\my_words.txt');
      DeleteFile(App + '\appearance.json');
      DeleteFile(App + '\config.yaml');
      RemoveDir(App);
    end;
  end;
end;
