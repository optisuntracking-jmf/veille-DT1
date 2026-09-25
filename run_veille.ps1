<#
.SYNOPSIS
    Lance l'automate de veille DT1 puis affiche une notification Windows
    avec le résultat (nouvelles entrées, fiches mises à jour, ou erreur).

.DESCRIPTION
    C'est ce script — et non plus main.py directement — que la tâche
    planifiée Windows doit exécuter (voir register_task.ps1), afin que
    chaque exécution hebdomadaire se termine par une notification visible,
    qu'elle ait réussi, échoué, ou ne trouvé aucune nouveauté.

.PARAMETER DryRun
    Transmis tel quel à main.py --dry-run : aucune écriture, mais la
    notification et le résumé sont quand même produits, pour pouvoir
    valider le comportement sans risque.

.EXAMPLE
    .\run_veille.ps1
    .\run_veille.ps1 -DryRun
#>

param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$MainScript = Join-Path $ProjectDir "main.py"
$SummaryPath = Join-Path $ProjectDir "logs\last_run_summary.json"

function Show-ToastNotification {
    param(
        [string]$Title,
        [string]$Message
    )
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

        $escapedTitle = [System.Security.SecurityElement]::Escape($Title)
        $escapedMessage = [System.Security.SecurityElement]::Escape($Message)
        $template = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>$escapedTitle</text>
      <text>$escapedMessage</text>
    </binding>
  </visual>
</toast>
"@
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml($template)
        $toast = New-Object Windows.UI.Notifications.ToastNotification $xml

        # AUMID de Windows PowerShell : fonctionne "out of the box" sur
        # Windows 10/11 sans enregistrement d'application ni module externe.
        $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    }
    catch {
        Write-Warning "Notification Windows impossible ($_) — le résultat reste disponible dans logs\veille.log"
    }
}

if (-not (Test-Path $PythonExe)) {
    Show-ToastNotification -Title "Veille DT1 — erreur de configuration" `
        -Message "Environnement virtuel introuvable ($PythonExe)."
    throw "Environnement virtuel introuvable ($PythonExe)."
}

$scriptArgs = @($MainScript)
if ($DryRun) { $scriptArgs += "--dry-run" }

& $PythonExe @scriptArgs
$exitCode = $LASTEXITCODE

$summary = $null
if (Test-Path $SummaryPath) {
    try { $summary = Get-Content $SummaryPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { Write-Warning "Résumé JSON illisible : $_" }
}

if ($exitCode -ne 0) {
    $detail = if ($summary -and $summary.error) { $summary.error } else { "code $exitCode" }
    Show-ToastNotification -Title "Veille DT1 — échec de l'exécution" `
        -Message "$detail`nVoir logs\veille.log pour le détail."
}
elseif ($null -eq $summary) {
    Show-ToastNotification -Title "Veille DT1" `
        -Message "Exécution terminée, mais le résumé est introuvable. Voir logs\veille.log."
}
else {
    $mode = if ($summary.dry_run) { "Test (dry-run, rien n'a été écrit)" } else { "Exécution réelle" }
    $ficheCount = @($summary.fiches_updated).Count
    $entryCount = @($summary.new_entries).Count

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add($mode)

    if ($ficheCount -eq 0 -and $entryCount -eq 0) {
        $lines.Add("Aucune nouveauté cette semaine.")
    }
    else {
        if ($ficheCount -gt 0) { $lines.Add("$ficheCount fiche(s) mise(s) a jour") }
        if ($entryCount -gt 0) { $lines.Add("$entryCount nouvelle(s) entree(s) en section 5") }
    }

    if ($summary.warnings_count -gt 0) {
        $lines.Add("$($summary.warnings_count) avertissement(s) - voir logs\veille.log")
    }

    Show-ToastNotification -Title "Veille scientifique DT1 terminee" -Message ($lines -join "`n")
}

exit $exitCode
