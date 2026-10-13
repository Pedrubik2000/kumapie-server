# Japanese words on the PC with the phones' own splitter (Jiten's, ported to Kotlin in kumapie's :lang; kumapie_plan.md
# item 26b): gradlew :lang:jitenSplit (JitenSplitCli) with the dictionaries from config.toml [kumapie.splitter].
#   tools\kumapie\jiten_split.ps1 files <x.kumapie.json>...        split episodes again, in place (<file>.bak: the original)
#   tools\kumapie\jiten_split.ps1 lines <texts.json> <out.json>    per text [[surface, offset, key or null, null]] (ja_words.py)
$ErrorActionPreference = "Stop"
$code = (Resolve-Path "$PSScriptRoot\..\..").Path
$paths = & "$code\.venv\Scripts\python.exe" -c @"
import sys, pathlib; sys.path.insert(0, r'$code')
from dojo import cfg; import sudachidict_core
s = cfg.raw['kumapie']['splitter']
for p in (cfg.root / s['jdk'], cfg.root / s['lexicon'], cfg.root / s['user_dic'],
          pathlib.Path(sudachidict_core.__file__).parent / 'resources' / 'system.dic', cfg.work / 'tmp', cfg.kumapie_app):
    print(p)
"@
if ($LASTEXITCODE) { throw "couldn't read config.toml [kumapie.splitter]" }
$env:JAVA_HOME, $env:KUMAPIE_JITEN_LEXICON, $env:KUMAPIE_SUDACHI_USER, $env:KUMAPIE_SUDACHI_DIC, $tmp, $app = $paths
$env:TEMP = $env:TMP = $tmp  # Gradle on Windows needs a short TEMP
$quoted = ($args | ForEach-Object { "'$_'" }) -join " "
& "$app\gradlew.bat" -q -p "$app" :lang:jitenSplit "--args=$quoted"
exit $LASTEXITCODE
