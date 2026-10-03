"""Read deadlines cover stalled bodies without changing write or streaming calls."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from ui.controls import controls_js


def test_shared_reads_bound_body_and_preserve_write_stream_contracts() -> None:
    runtime = controls_js()
    assert "/* Bounded UI reads */" in runtime
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for request lifecycle evidence")
    runtime = runtime.split("/* Bounded UI reads */", 1)[1]
    harness = (
        r"""
global.window = {location:new URL('http://localhost/'), setTimeout, clearTimeout};
const observations = [];
global.CustomEvent = class { constructor(name, options) { this.detail=options.detail; } };
global.document = {dispatchEvent:event=>observations.push(event.detail)};
"""
        + runtime
        + r"""
(async()=>{
  let calls=0;
  global.fetch = async (_url, options) => {
    calls++;
    return new Response(new ReadableStream({
      start(controller) {
        options.signal.addEventListener('abort',()=>controller.error(options.signal.reason));
      }
    }));
  };
  try { await window.uiFetch('/api/test', {timeoutMs:20}); throw Error('hung body accepted'); }
  catch(error) { if(error.name !== 'TimeoutError') throw error; }
  if(calls!==1) throw Error('unrequested retry');
  const controller=new AbortController(); controller.abort();
  try { await window.uiFetch('/api/test', {signal:controller.signal}); throw Error('abort accepted'); }
  catch(error) { if(error.name !== 'AbortError') throw error; }
  if(calls!==1) throw Error('cancelled request sent');
  global.fetch=async()=>new Response('secret error body',{status:503});
  try {await window.uiFetch('/api/test'); throw Error('HTTP failure accepted');}
  catch(error) {if(error.status!==503 || error.message.includes('secret')) throw error;}
  const write=new Response('write');
  global.fetch=async(_url,options)=>{if(options.timeoutMs!==undefined)throw Error('custom option leaked');return write;};
  if(await window.uiFetch('/api/write',{method:'POST',timeoutMs:20})!==write)throw Error('write contract changed');
  const stream=new Response('stream',{headers:{'Content-Type':'text/event-stream'}});
  global.fetch=async()=>stream;
  if(await window.uiFetch('/api/events',{headers:{Accept:'text/event-stream'}})!==stream)throw Error('stream buffered');
  if(await window.uiFetch(new Request('http://localhost/api/events',{headers:{Accept:'text/event-stream'}}))!==stream)throw Error('Request stream buffered');
  let streamCancelled=false;
  global.fetch=async()=>new Response(new ReadableStream({cancel(){streamCancelled=true;}}),{headers:{'Content-Type':'text/event-stream'}});
  try {await window.uiFetch('/api/test');throw Error('unexpected stream accepted');}
  catch(error) {if(!error.message.includes('Accept:'))throw error;}
  if(!streamCancelled)throw Error('unexpected stream left open');
  global.fetch=async()=>new Response('value',{headers:{ETag:'saved'}});
  const result=await window.uiFetch('/api/test?private=hidden');
  if(await result.text()!=='value'||result.headers.get('ETag')!=='saved')throw Error('response changed');
  if(observations.some(item=>JSON.stringify(item).includes('private')))throw Error('private URL exposed');
  if(await (await window.uiFetch(new URL('http://localhost/api/test'))).text()!=='value')throw Error('URL input unsupported');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
