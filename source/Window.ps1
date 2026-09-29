param([string]$Backend,[string]$BackendScript='',[string]$DataDir,[string]$SmokeOutput='',[switch]$StartMonitoring)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
Add-Type -TypeDefinition @"
using System.Windows.Forms;
public class SmoothListView : ListView {
 public SmoothListView() { DoubleBuffered = true; }
}
"@ -ReferencedAssemblies System.Windows.Forms
$script:queued=$null; $script:job=$null; $script:rows=@(); $script:chosen=@{}; $script:states=@{}; $script:details=@{}
$script:cancelStart=$false; $script:running=$false; $script:rendering=$false; $script:dirty=$false; $script:pollSeconds=30
$script:lastTick=[DateTime]::Now; $script:autoStart=[bool]$StartMonitoring; $script:notices=@{}
$form=New-Object Windows.Forms.Form -Property @{Text='Codex 自动续接助手 · 通用版';Width=1280;Height=920;MinimumSize=(New-Object Drawing.Size(1120,800));StartPosition='CenterScreen';Font=(New-Object Drawing.Font('Microsoft YaHei UI',10))}
$layout=New-Object Windows.Forms.TableLayoutPanel -Property @{Dock='Fill';ColumnCount=1;RowCount=8;Padding=(New-Object Windows.Forms.Padding(16))}
foreach($h in @(76,52,52)){[void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('Absolute',$h)))}
[void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('Percent',100)))
foreach($h in @(50,58,80,120)){[void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('Absolute',$h)))}
$form.Controls.Add($layout)
function Label($text){New-Object Windows.Forms.Label -Property @{Text=$text;AutoSize=$true;Padding=(New-Object Windows.Forms.Padding(3,7,3,0))}}
function Bar($row){$p=New-Object Windows.Forms.FlowLayoutPanel -Property @{Dock='Fill'};$layout.Controls.Add($p,0,$row);return $p}
function Button($text,$width){New-Object Windows.Forms.Button -Property @{Text=$text;Width=$width;Height=32}}
$heading=Label '让对话继续，让进度留下。';$heading.Font=New-Object Drawing.Font('Microsoft YaHei UI',24,[Drawing.FontStyle]::Bold);$layout.Controls.Add($heading,0,0)
$settings=Bar 1
$mode=New-Object Windows.Forms.ComboBox -Property @{DropDownStyle='DropDownList';Width=125};[void]$mode.Items.AddRange(@('单对话','多对话'));$mode.SelectedIndex=0;$settings.Controls.Add($mode)
$settings.Controls.Add((Label '检查间隔（秒）'))
$interval=New-Object Windows.Forms.NumericUpDown -Property @{Minimum=10;Maximum=86400;Value=30;Width=110};$settings.Controls.Add($interval)
$settings.Controls.Add((Label '重试等待（秒）'))
$retryWait=New-Object Windows.Forms.NumericUpDown -Property @{Minimum=5;Maximum=86400;Value=30;Width=95};$settings.Controls.Add($retryWait)
$apply=Button '保存配置' 120;$settings.Controls.Add($apply)
$settings.Controls.Add((Label '每次重试使用此等待时间；保存后开始'))
$searchBar=Bar 2
$search=New-Object Windows.Forms.TextBox -Property @{Width=480};$searchBar.Controls.Add($search)
$refresh=Button '刷新聊天列表' 140;$searchBar.Controls.Add($refresh);$searchBar.Controls.Add((Label '搜索名称、项目或 ID；勾选需要监测的聊天'))
$list=New-Object SmoothListView -Property @{Dock='Fill';View='Details';FullRowSelect=$true;MultiSelect=$false;CheckBoxes=$true;HideSelection=$false}
foreach($c in @(@('聊天',250),@('项目',100),@('任务状态',90),@('监控状态',105),@('最近活动',115),@('重试',60),@('监测结果',380))){[void]$list.Columns.Add($c[0],$c[1])}
$layout.Controls.Add($list,0,3)
$idBar=Bar 4;$idBar.Controls.Add((Label '聊天 ID'))
$idBox=New-Object Windows.Forms.TextBox -Property @{Width=480};$idBar.Controls.Add($idBox)
$add=Button '核验并加入' 130;$idBar.Controls.Add($add)
$approval=New-Object Windows.Forms.CheckBox -Property @{Text='为高亮聊天启用限定授权';Width=260;Height=32};$idBar.Controls.Add($approval)
$controls=Bar 5
$start=Button '开始 / 恢复监测' 170;$controls.Controls.Add($start)
$pause=Button '暂停全部监测' 145;$controls.Controls.Add($pause)
$openLog=Button '打开日志目录' 140;$controls.Controls.Add($openLog)
$status=New-Object Windows.Forms.Label -Property @{Dock='Fill';Text='尚未开始。首次使用请勾选聊天并保存配置。'};$layout.Controls.Add($status,0,6)
$log=New-Object Windows.Forms.TextBox -Property @{Dock='Fill';Multiline=$true;ReadOnly=$true;ScrollBars='Vertical'};$layout.Controls.Add($log,0,7)
$notify=New-Object Windows.Forms.NotifyIcon -Property @{Icon=[Drawing.SystemIcons]::Information;Visible=$true;Text='Codex 自动续接助手'}
function Log($text){$log.AppendText(('['+[DateTime]::Now.ToString('HH:mm:ss')+'] '+$text+"`r`n"))}
function Pause-Now { [void][IO.Directory]::CreateDirectory($DataDir);[IO.File]::WriteAllText((Join-Path $DataDir 'paused'),'');$script:running=$false;$script:autoStart=$false;$script:cancelStart=$true;$script:queued=$null;Update-MonitorLabels }
function Changed {if(-not $script:rendering){Pause-Now;$script:dirty=$true;$status.Text='配置已修改，发送已暂停。请保存配置，然后开始监测。'}}
function Quote($s){if($s.Contains('"')){throw '路径不能包含引号'};return '"'+($s -replace '(\\+)$','$1$1')+'"'}
function Run($action,$value=$null){
 if($script:job){if($action -notin @('tick','observe')){$script:queued=@{action=$action;value=$value}};return};if($action -eq 'start'){$script:cancelStart=$false}
 $out=Join-Path ([IO.Path]::GetTempPath()) ('codex-monitor-'+[Guid]::NewGuid().ToString('N')+'.json')
 $inputFile=$out+'.input';$parts=@();if($BackendScript){$parts+=(Quote $BackendScript)}
 $parts+=@('--action',$action,'--output-file',(Quote $out),'--data-dir',(Quote $DataDir))
 if($null -ne $value){[IO.File]::WriteAllText($inputFile,($value|ConvertTo-Json -Depth 8 -Compress),(New-Object Text.UTF8Encoding($false)));$parts+=@('--value-file',(Quote $inputFile))}
 $info=New-Object Diagnostics.ProcessStartInfo -Property @{FileName=$Backend;Arguments=($parts -join ' ');UseShellExecute=$false;CreateNoWindow=$true}
 $p=New-Object Diagnostics.Process;$p.StartInfo=$info;[void]$p.Start()
 $script:job=@{process=$p;output=$out;input=$inputFile;action=$action;started=[DateTime]::Now}
 if($action -notin @('tick','observe')){foreach($c in @($start,$apply,$refresh,$add,$approval,$mode,$interval,$retryWait,$list)){if($c -isnot [Windows.Forms.Control]){throw ('Invalid control: '+($c|Out-String))};$c.Enabled=$false}}
}
function Monitor-Label($id){
 if(-not $script:chosen.ContainsKey($id)){return '未加入'}
 if(-not $script:running){return '已暂停'}
 $s=$script:states[$id];$detail=[string]$script:details[$id]
 if($s.stopped){return '已停止'}
 if($detail -match '连接或状态读取失败|本地接口需要另一个聊天'){return '等待重连'}
 if($s.pending){return '等待回执'}
 if($detail -match '等待你在原对话审核'){return '等待审核'}
 return '监测中'
}
function Update-MonitorLabels {
 if($list){foreach($item in $list.Items){if($item.SubItems.Count -gt 3){$item.SubItems[3].Text=Monitor-Label ([string]$item.Tag)}}}
}
function Detail-Label($id, [double]$nowSeconds=([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0)){
 $s=$script:states[$id];$detail=[string]$script:details[$id]
 if($script:running -and $script:chosen.ContainsKey($id) -and $s.due -and -not $s.pending -and -not $s.stopped){
  $remaining=[Math]::Max(0,[Math]::Ceiling([double]$s.due-$nowSeconds))
  $when=[DateTimeOffset]::FromUnixTimeSeconds([long]$s.due).LocalDateTime.ToString('HH:mm:ss')
  if($remaining -eq 0){return ('已到重试时间，等待本轮核验 · '+$when)}
  return ('还剩 '+$remaining+' 秒 · 预计 '+$when+' · '+$detail)
 }
 return $detail
}
function Poll-Delay {
 if($script:running){foreach($id in $script:chosen.Keys){if($script:states[$id].pending -and -not $script:states[$id].stopped){return [Math]::Min(5,$script:pollSeconds)}}}
 $delay=[double]$script:pollSeconds
 if($script:running){$now=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0;foreach($id in $script:chosen.Keys){$s=$script:states[$id];if($s.due -and -not $s.stopped -and -not $s.pending){$delay=[Math]::Min($delay,[Math]::Max(1,[double]$s.due-$now+([DateTime]::Now-$script:lastTick).TotalSeconds))}}}
 return $delay
}
function Update-Countdown {
 foreach($item in $list.Items){if($item.SubItems.Count -gt 6){$text=Detail-Label ([string]$item.Tag);if($item.SubItems[6].Text -ne $text){$item.SubItems[6].Text=$text};$item.ToolTipText=$text}}
 if($script:dirty){return}
 $next=[Math]::Max(0,[Math]::Ceiling((Poll-Delay)-([DateTime]::Now-$script:lastTick).TotalSeconds))
 $cycle=if($script:job){'正在检查…'}elseif($script:running){'下次检查还有 '+$next+' 秒'}else{'自动发送已暂停'}
 $newStatus=($(if($script:running){'监测中'}else{'已暂停'})+' | 已选 '+$script:chosen.Count+' 个聊天 | 间隔 '+$script:pollSeconds+' 秒 | '+$cycle+"`r`n"+'每个聊天独立计数；需要审核时在原对话处理。检查轮次不会重叠。');if($status.Text -ne $newStatus){$status.Text=$newStatus}
}
function Update-Rows {
 $byId=@{};foreach($row in $script:rows){$byId[$row.id]=$row}
 $names=@{active='执行中';idle='空闲';systemError='服务异常';notLoaded='未加载';failed='失败';completed='已结束';inProgress='执行中';interrupted='已中止'}
 foreach($item in $list.Items){
  $id=[string]$item.Tag;$row=$byId[$id];if(-not $row){continue};$s=$script:states[$id]
  $friendly=$names[[string]$row.status];if(-not $friendly){$friendly=[string]$row.status}
  $stamp=[double]$row.updated;if($stamp -gt 100000000000){$stamp/=1000}
  $when=if($stamp -gt 0){[DateTimeOffset]::FromUnixTimeSeconds([long]$stamp).LocalDateTime.ToString('MM-dd HH:mm')}else{''}
  $cells=@([string]$row.title,[string]$row.project,$friendly,(Monitor-Label $id),$when,$(if($s){[string]$s.attempts+'/6'}else{'—'}),(Detail-Label $id))
  for($index=0;$index -lt $cells.Count;$index++){if($item.SubItems[$index].Text -ne $cells[$index]){$item.SubItems[$index].Text=$cells[$index]}}
 }
}
function Render {
 $highlight=if($list.SelectedItems.Count -eq 1){[string]$list.SelectedItems[0].Tag}else{''};$script:rendering=$true;$list.BeginUpdate();$list.Items.Clear()
 foreach($row in $script:rows){
  if($search.Text -and (($row.title+' '+$row.project+' '+$row.id).IndexOf($search.Text,[StringComparison]::OrdinalIgnoreCase) -lt 0)){continue}
  $item=New-Object Windows.Forms.ListViewItem([string]$row.title);$item.Tag=$row.id;$item.Checked=$script:chosen.ContainsKey($row.id)
  [void]$item.SubItems.Add([string]$row.project);$statusNames=@{active='执行中';idle='空闲';systemError='服务异常';notLoaded='未加载';failed='失败';completed='已结束';inProgress='执行中';interrupted='已中止'};$friendly=$statusNames[[string]$row.status];if(-not $friendly){$friendly=[string]$row.status};[void]$item.SubItems.Add($friendly);[void]$item.SubItems.Add((Monitor-Label $row.id))
  $stamp=[double]$row.updated;if($stamp -gt 100000000000){$stamp/=1000}
  $when=if($stamp -gt 0){[DateTimeOffset]::FromUnixTimeSeconds([long]$stamp).LocalDateTime.ToString('MM-dd HH:mm')}else{''}
  [void]$item.SubItems.Add($when);$s=$script:states[$row.id];[void]$item.SubItems.Add($(if($s){[string]$s.attempts+'/6'}else{'—'}))
  [void]$item.SubItems.Add((Detail-Label $row.id));if($item.Checked){$item.BackColor=[Drawing.Color]::FromArgb(235,243,255)}elseif($list.Items.Count % 2 -eq 1){$item.BackColor=[Drawing.Color]::FromArgb(248,250,253)};[void]$list.Items.Add($item);if($row.id -eq $highlight){$item.Selected=$true}
 }
 $list.EndUpdate();$script:rendering=$false
}
function Save-Settings {
 if($mode.SelectedIndex -eq 0 -and $script:chosen.Count -gt 1){Log '单对话模式只能勾选一个聊天';return}
 Run 'configure' @{ids=@($script:chosen.Keys);interval=[int]$interval.Value;retry_seconds=[int]$retryWait.Value;mode=$(if($mode.SelectedIndex -eq 0){'single'}else{'multi'})}
}
$search.Add_TextChanged({Render})
$list.Add_ItemChecked({param($sender,$e)
 if($script:rendering){return};$id=[string]$e.Item.Tag
 if($e.Item.Checked){
  if($mode.SelectedIndex -eq 0){$script:chosen.Clear();$script:rendering=$true;foreach($other in $list.Items){if($other -ne $e.Item){$other.Checked=$false}};$script:rendering=$false}
  $script:chosen[$id]=$true
 }else{$script:chosen.Remove($id)}
 Changed
})
$list.Add_SelectedIndexChanged({if(-not $script:rendering -and $list.SelectedItems.Count -eq 1){$id=[string]$list.SelectedItems[0].Tag;$idBox.Text=$id;$script:rendering=$true;$approval.Checked=[bool]$script:states[$id].approval;$script:rendering=$false}})
$mode.Add_SelectedIndexChanged({Changed})
$interval.Add_ValueChanged({Changed});$retryWait.Add_ValueChanged({Changed})
$apply.Add_Click({Save-Settings})
$add.Add_Click({$id=$idBox.Text.Trim();$parsed=[Guid]::Empty;if(-not [Guid]::TryParse($id,[ref]$parsed)){Log '聊天 ID 格式错误';return};Changed;if($mode.SelectedIndex -eq 0){$script:chosen.Clear()};$script:chosen[$id]=$true;Save-Settings})
$refresh.Add_Click({if($script:dirty){Log '请先保存配置，再刷新列表'}else{Run 'list'}})
$start.Add_Click({if($script:dirty){Log '请先保存配置'}else{Run 'start'}})
$pause.Add_Click({Pause-Now;$status.Text='已暂停全部监测';Log '已暂停；尚未提交的消息不会发送'})
$approval.Add_CheckedChanged({if(-not $script:rendering){if($list.SelectedItems.Count -ne 1 -or $script:dirty){Log '请先保存配置并高亮选择一个聊天';return};Run 'approval' @{id=[string]$list.SelectedItems[0].Tag;enabled=[bool]$approval.Checked}}})
$openLog.Add_Click({Start-Process explorer.exe -ArgumentList (Quote $DataDir)})
# Calm, high-contrast desktop styling with clear action hierarchy.
$form.BackColor=[Drawing.Color]::FromArgb(244,247,252)
$form.ForeColor=[Drawing.Color]::FromArgb(25,40,64)
$layout.Padding=New-Object Windows.Forms.Padding(24,18,24,18)
$heading.ForeColor=[Drawing.Color]::FromArgb(20,43,79)
foreach($b in @($start,$pause,$apply,$refresh,$add,$openLog)){
 $b.FlatStyle='Flat';$b.FlatAppearance.BorderColor=[Drawing.Color]::FromArgb(207,218,234)
 $b.BackColor=[Drawing.Color]::White;$b.ForeColor=[Drawing.Color]::FromArgb(35,58,91)
 $b.Height=36;$b.Cursor=[Windows.Forms.Cursors]::Hand;$b.Margin=New-Object Windows.Forms.Padding(3,2,10,2)
}
$start.Text='开始 / 恢复监测';$start.BackColor=[Drawing.Color]::FromArgb(40,98,220);$start.ForeColor=[Drawing.Color]::White;$start.FlatAppearance.BorderSize=0
$apply.BackColor=[Drawing.Color]::FromArgb(225,235,252);$apply.FlatAppearance.BorderSize=0
$pause.ForeColor=[Drawing.Color]::FromArgb(154,72,51)
$list.BorderStyle='None';$list.BackColor=[Drawing.Color]::White;$list.ShowItemToolTips=$true
$rowHeight=New-Object Windows.Forms.ImageList;$rowHeight.ImageSize=New-Object Drawing.Size(1,32);$list.SmallImageList=$rowHeight
$status.BackColor=[Drawing.Color]::FromArgb(230,239,251);$status.Padding=New-Object Windows.Forms.Padding(15,13,12,8)
$status.Margin=New-Object Windows.Forms.Padding(0,2,0,10)
$log.BorderStyle='None';$log.BackColor=[Drawing.Color]::White;$log.ForeColor=[Drawing.Color]::FromArgb(91,109,136)
$log.Font=New-Object Drawing.Font('Microsoft YaHei UI',9)
$search.BorderStyle='FixedSingle';$idBox.BorderStyle='FixedSingle'
$timer=New-Object Windows.Forms.Timer -Property @{Interval=400}
$timer.Add_Tick({try{Update-Countdown
 if($script:job -and ([DateTime]::Now-$script:job.started).TotalSeconds -gt (90+90*$script:chosen.Count)){
  $script:job.process.Kill();[void]$script:job.process.WaitForExit(2000)
  [IO.File]::WriteAllText($script:job.output,'{"ok":false,"disconnected":true,"error":"后台超时，等待重连"}',[Text.Encoding]::UTF8)
 }
 if($script:job -and $script:job.process.HasExited){
  $j=$script:job;$script:job=$null;$j.process.Dispose();if($j.action -notin @('tick','observe')){foreach($c in @($start,$apply,$refresh,$add,$approval,$mode,$interval,$retryWait,$list)){$c.Enabled=$true}}
  $script:lastTick=[DateTime]::Now
  try{$r=[IO.File]::ReadAllText($j.output,[Text.Encoding]::UTF8)|ConvertFrom-Json}finally{foreach($f in @($j.output,$j.input)){if([IO.File]::Exists($f)){[IO.File]::Delete($f)}}}
  if(-not $r.ok){if($r.disconnected){foreach($id in $script:chosen.Keys){$script:details[$id]='连接或状态读取失败，等待下次检查'};Update-Rows};Log $r.error;$status.Text='等待处理 / 重连：'+$r.error;if($SmokeOutput){throw $r.error}}
  else{
   $d=$r.data
   switch($j.action){
    'list' {
     $script:rows=@($d.rows);$script:chosen=@{};foreach($id in $d.targets){$script:chosen[$id]=$true}
     $script:states=@{};foreach($p in $d.states.PSObject.Properties){$script:states[$p.Name]=$p.Value}
     $script:rendering=$true;$interval.Value=$d.interval;$retryWait.Value=$d.retry_seconds;$script:pollSeconds=[int]$d.interval;$mode.SelectedIndex=$(if($d.mode -eq 'multi'){1}else{0});$script:rendering=$false;Render
     if($script:autoStart){$script:autoStart=$false;Run 'start'}else{Run 'observe'}
    }
    'configure' {$script:dirty=$false;Log $d.reason;Run 'list'}
    'start' {if($script:cancelStart){Pause-Now};if(-not [IO.File]::Exists((Join-Path $DataDir 'paused'))){$script:running=$true};Log $d.reason;Run 'tick'}
    'approval' {Log $d.reason;Run 'list'}
    default {
     foreach($result in $d.results){
      if($result.state){$script:states[$result.id]=$result.state};$script:details[$result.id]=$result.reason
      if($result.snapshot){foreach($row in $script:rows){if($row.id -eq $result.id){if($result.snapshot.updated){$row.updated=$result.snapshot.updated};$row.status=$(if($result.snapshot.running){'active'}else{$result.snapshot.turn_status})}}}
      if($result.notify -and $script:notices[$result.id] -ne $result.reason){$script:notices[$result.id]=$result.reason;Log ($result.id+' '+$result.reason);$notify.ShowBalloonTip(6000,'聊天需要处理',[string]$result.reason,[Windows.Forms.ToolTipIcon]::Info)}
     }
     if($d.paused){$script:running=$false};Update-Rows
     Update-Countdown
     if($SmokeOutput){$stableRows=$true;if($list.Items.Count -gt 3){$probe=$list.Items[3];$script:rendering=$true;$probe.Selected=$true;$script:rendering=$false;$list.TopItem=$probe;$oldTop=$list.TopItem;Update-Rows;$stableRows=([object]::ReferenceEquals($probe,$list.Items[3]) -and $probe.Selected -and [object]::ReferenceEquals($oldTop,$list.TopItem));if(-not $stableRows){throw '局部刷新未保留行或滚动位置'}};$savedRunning=$script:running;$script:chosen['__clock_test__']=$true;$script:states['__clock_test__']=@{due=100;pending=$null;stopped=$false};$script:running=$true;$a=Detail-Label '__clock_test__' 80;$b=Detail-Label '__clock_test__' 81;$z=Detail-Label '__clock_test__' 110;$script:states['__clock_test__'].pending=@{marker='test'};$fastPoll=((Poll-Delay) -eq 5);$script:running=$false;$p=Detail-Label '__clock_test__' 80;$script:chosen.Remove('__clock_test__');$script:states.Remove('__clock_test__');$script:running=$savedRunning;$clockPassed=($a -match '20 秒' -and $b -match '19 秒' -and $z -match '已到' -and $p -notmatch '还剩' -and $fastPoll);if(-not $clockPassed){throw '倒计时测试失败'};if(@($d.results|Where-Object {-not $_.snapshot}).Count -gt 0){throw '真实聊天状态读取失败'};$monitorLabels=@($list.Items|Where-Object {$_.Checked}|ForEach-Object {$_.SubItems[3].Text});$beforeSearch=$script:chosen.Count;$search.Text='__no_matching_chat_for_smoke__';$emptySearch=($list.Items.Count -eq 0);$search.Text='';$searchPreserved=($beforeSearch -eq $script:chosen.Count);$bmp=New-Object Drawing.Bitmap($form.Width,$form.Height);$form.DrawToBitmap($bmp,$form.ClientRectangle);$bmp.Save($SmokeOutput+'.png');$bmp.Dispose();@{ok=$true;stableRows=$stableRows;retry_seconds=[int]$retryWait.Value;clockPassed=$clockPassed;monitorLabels=$monitorLabels;searchEmpty=$emptySearch;searchPreserved=$searchPreserved;rows=$list.Items.Count;targets=$script:chosen.Count;interval=$script:pollSeconds;status=$status.Text}|ConvertTo-Json|Set-Content ($SmokeOutput+'.json') -Encoding UTF8;$form.Close()}
    }
   }
  }
 }
 if(-not $script:job -and $script:queued){$nextJob=$script:queued;$script:queued=$null;Run $nextJob.action $nextJob.value}
 if(-not $script:job -and ([DateTime]::Now-$script:lastTick).TotalSeconds -ge (Poll-Delay)){if($script:running){Run 'tick'}else{Run 'observe'}}
}catch{Log ('异常：'+$_.Exception.Message);$script:lastTick=[DateTime]::Now;if($SmokeOutput){@{ok=$false;error=$_.Exception.Message;stack=$_.ScriptStackTrace}|ConvertTo-Json|Set-Content ($SmokeOutput+'.json') -Encoding UTF8;$form.Close()}}})
$form.Add_Shown({Log '首次启动不自动发送。勾选聊天、设置间隔、保存配置后开始。';Run 'list';$timer.Start()})
$form.Add_FormClosing({Pause-Now})
[void]$form.ShowDialog();$timer.Stop();$notify.Visible=$false;$notify.Dispose()
if($script:job){if(-not $script:job.process.WaitForExit(20000)){$script:job.process.Kill()};$script:job.process.Dispose()}
$form.Dispose()
