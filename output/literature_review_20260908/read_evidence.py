from pathlib import Path
import re,sys
sys.stdout.reconfigure(encoding='utf-8')
root=Path(__file__).resolve().parent/'sources'
for prefix in sys.argv[1:]:
    fs=list(root.glob(prefix+'*.txt'))
    for f in fs:
        s=f.read_text(encoding='utf-8')
        print('\nFILE',f.name,'TOTAL',len(s))
        print(s[:900])
        used=[]
        for pattern in [r'(?im)^.{0,8}(?:[A-Z][A-Za-z -]* )?(?:Methodology|Approach|Framework|Benchmark Construction|Data Collection|Experimental Setup|Study Design)\b',r'(?im)^.{0,8}(?:Dataset|Subject Models|Studied Models|Evaluation Metric|Benchmarks|Data Preparation)\b',r'(?im)^.{0,10}(?:Table [1234]|TABLE [IVX]+)',r'(?im)^.{0,10}(?:Threats to Validity|Limitations|Conclusion)\b']:
            ms=list(re.finditer(pattern,s))
            for m in ms[:1]:
                a=max(0,m.start()-60);b=min(len(s),m.start()+1250)
                if any(a<x2 and b>x1 for x1,x2 in used):continue
                used.append((a,b))
                print('\nEXCERPT AT',a,'\n',s[a:b])
        urls=re.findall(r'https?://[^\s<>]+',s)
        print('LINKS', ' '.join(x for x in urls if 'github' in x or 'doi.' in x or 'zenodo' in x)[:1200])
