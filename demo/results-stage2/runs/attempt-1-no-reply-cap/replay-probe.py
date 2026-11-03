import json, time, urllib.request
msgs = json.load(open('msgs.json'))
for cap in (1200,):
    payload = {"model": "qwen2.5-coder:7b", "messages": msgs, "stream": False,
               "format": "json",
               "options": {"temperature": 0.0, "num_ctx": 16384, "num_predict": cap}}
    t = time.time()
    req = urllib.request.Request("http://localhost:11434/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=900).read())
    el = time.time() - t
    print(f"cap={cap} seconds={el:.1f} done_reason={d.get('done_reason')!r} "
          f"prompt_eval={d.get('prompt_eval_count')} eval={d.get('eval_count')}")
    content = d["message"]["content"]
    print("content length:", len(content))
    print(content[:1200])
    print("...TAIL...")
    print(content[-400:])
