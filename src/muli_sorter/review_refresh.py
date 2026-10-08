"""Label bounded cached displays without replacing an operator's unsaved work."""
from datetime import datetime, timezone


def display_html(html, generation, verified_at):
    stamp = datetime.fromtimestamp(verified_at, timezone.utc).isoformat()
    banner = ('<aside id="review-refresh" class="notice" role="status" style="margin:1rem">'
              '<span id="review-refresh-text">正在后台更新；先显示上次核验的结果。'
              '提交归档前会重新检查。上次核验：<time id="review-verified-time" datetime="' + stamp + '">'
              + stamp + '</time></span> '
              '<button id="review-load-latest" hidden type="button">加载最新结果</button></aside>')
    script = '''<script>(function(){
      var button=document.getElementById('review-load-latest'), text=document.getElementById('review-refresh-text');
      var stamp=document.getElementById('review-verified-time');
      if(stamp)stamp.textContent=new Date(stamp.dateTime).toLocaleString();
      button.addEventListener('click',function(){
        try {if(!window.muliReview||window.muliReview.saveDraft()!==true)throw new Error('草稿未保存');}
        catch(e){text.textContent='当前选择保存失败，请先导出计划，再加载最新结果。';return;}
        window.location.reload();
      });
      var attempts=0;
      async function poll(){
        if(++attempts>30){text.textContent='后台更新仍未完成；当前为上次核验结果，可稍后重新加载。';button.hidden=false;return;}
        var controller=new AbortController(), deadline=setTimeout(function(){controller.abort();},5000);
        try {
          var response=await fetch('/api/review-status',{cache:'no-store',signal:controller.signal});
          if(response.status===401||response.status===403){text.textContent='更新需要重新确认访问权限。';return;}
          if(!response.ok)throw new Error('读取更新状态失败');
          var state=await response.json();
          if(state.requires_reload||state.ready_generation>GENERATION){
            text.textContent=state.requires_reload?'素材清单有变化，请加载最新结果；当前选择会先保存为草稿。':'最新核验结果已准备；当前选择保留，加载最新结果前会保存草稿。';button.hidden=false;return;
          }
          if(state.status==='failed'){text.textContent='后台更新暂未完成；当前为上次核验结果，可稍后重新加载。';button.hidden=false;return;}
        }catch(e){}finally{clearTimeout(deadline);}
        setTimeout(poll,2000);
      }
      setTimeout(poll,500);
    })();</script>'''.replace('GENERATION', str(int(generation)))
    return html.replace(b'<body>', b'<body>' + banner.encode(), 1).replace(b'</body>', script.encode() + b'</body>', 1)
