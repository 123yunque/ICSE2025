from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import urllib.request, json, io
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parent
SOURCES={
'01_duplication':'https://arxiv.org/pdf/2401.07930',
'02_membership':'https://arxiv.org/pdf/2511.15107',
'04_paichecker':'https://arxiv.org/pdf/2607.28587',
'05_patchdiff':'https://software-lab.org/publications/icse2026_SWE-bench-correctness.pdf',
'06_reproducibility':'https://arxiv.org/pdf/2510.25506',
'07_reval':'https://xing-hu.github.io/assets/papers/ICSE2025CodeReasoning.pdf',
'08_tracecoder':'https://arxiv.org/pdf/2602.06875',
'10_seer':'https://arxiv.org/pdf/2510.17130',
'11_mgdebugger':'https://arxiv.org/pdf/2410.01215',
'14_judge':'https://arxiv.org/pdf/2507.16587',
'16_proxywar':'https://arxiv.org/pdf/2602.04296',
'17_coffe':'https://arxiv.org/pdf/2502.02827',
'19_adapteval':'https://arxiv.org/pdf/2601.04540',
'20_mcrbench':'https://arxiv.org/pdf/2608.27442',
'12_causality':'https://repository.hkust.edu.hk/ir/bitstream/1783.1-167759/1/1783.1-167759.pdf',
'13_turbulence':'https://www.doc.ic.ac.uk/~afd/papers/2025/TSE.pdf',
'15_local':'https://arxiv.org/pdf/2509.15397',
'18_realistic':'https://xing-hu.github.io/assets/papers/ase254.pdf',
}

def fetch(item):
    key,url=item
    p=ROOT/'sources'/f'{key}.pdf'
    p.parent.mkdir(parents=True,exist_ok=True)
    try:
        if not p.exists():
            with urllib.request.urlopen(url,timeout=45) as r:
                content=r.read()
            if not content.startswith(b'%PDF'): raise ValueError('not PDF')
            p.write_bytes(content)
        reader=PdfReader(p)
        text='\n'.join(f'\n===== PAGE {i+1} =====\n'+(pg.extract_text() or '') for i,pg in enumerate(reader.pages))
        p.with_suffix('.txt').write_text(text,encoding='utf-8',errors='replace')
        out={'id':key,'url':url,'pages':len(reader.pages),'chars':len(text),'status':'ok'}
    except Exception as e:
        out={'id':key,'url':url,'status':'error','error':str(e)}
    print(json.dumps(out,ensure_ascii=False),flush=True)
    return out

if __name__=='__main__':
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(fetch,SOURCES.items()))
    (ROOT/'source_manifest.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
