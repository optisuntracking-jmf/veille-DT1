<#
.SYNOPSIS
    Enregistre (ou remplace) la tâche planifiée Windows exécutant l'automate
    de veille scientifique DT1 chaque semaine.

.DESCRIPTION
    Crée une tâche planifiée nommée "VeilleDT1" qui lance run_veille.ps1
    (lequel appelle main.py puis affiche une notification Windows avec le
    résultat), une fois par semaine. Sans -DryRun, la tâche écrit réellement
    dans le fichier Markdown.

.PARAMETER DayOfWeek
    Jour d'exécution hebdomadaire (défaut : Monday).

.PARAMETER Time
    Heure d'exécution au format HH:mm (défaut : 07:00).

.PARAMETER DryRun
    Si présent, la tâche planifiée sera configurée pour tourner en mode
    --dry-run (aucune écriture, juste le log). Utile pour valider la
    planification avant de l'activer en écriture réelle.

.EXAMPLE
    .\register_task.ps1
    .\register_task.ps1 -DayOfWeek Wednesday -Time "08:30"
    .\register_task.ps1 -DryRun
#>

param(
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$DayOfWeek = "Monday",
    [string]$Time = "07:00",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$RunnerScript = Join-Path $ProjectDir "run_veille.ps1"
$TaskName = "VeilleDT1"

if (-not (Test-Path $PythonExe)) {
    throw "Environnement virtuel introuvable ($PythonExe). Exécutez d'abord : py -3 -m venv .venv ; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}
if (-not (Test-Path $RunnerScript)) {
    throw "run_veille.ps1 introuvable dans $ProjectDir."
}

$runnerArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$RunnerScript`""
if ($DryRun) {
    $runnerArgs += " -DryRun"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $runnerArgs -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Une tâche '$TaskName' existe déjà : elle va être remplacée."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Veille scientifique hebdomadaire DT1 (PubMed, ClinicalTrials.gov, RSS) - $ProjectDir" | Out-Null

Write-Host "Tâche planifiée '$TaskName' enregistrée : chaque $DayOfWeek à $Time."
if ($DryRun) {
    Write-Host "Mode dry-run actif : le fichier Markdown ne sera JAMAIS modifié par cette tâche tant que vous ne relancez pas ce script sans -DryRun."
} else {
    Write-Host "Mode écriture réelle : le fichier Markdown sera modifié chaque semaine. Le PC doit être allumé à l'heure prévue (StartWhenAvailable rattrape une exécution manquée au prochain démarrage)."
}
Write-Host "Une notification Windows s'affichera a la fin de chaque execution (resultat, ou erreur). Elle ne s'affiche que si vous etes alors connecte a une session (LogonType Interactive) ; sinon, consultez logs\veille.log et logs\last_run_summary.json."
Write-Host "Pour vérifier : Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "Pour supprimer : Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
