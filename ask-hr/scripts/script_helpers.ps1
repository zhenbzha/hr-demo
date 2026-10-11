function Invoke-Checked {
    param(
        [Parameter(Mandatory)][scriptblock] $Script,
        [Parameter(Mandatory)][string] $Description
    )

    $output = & $Script
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
    return $output
}
