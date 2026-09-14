from pathlib import Path
import json, re
from pypdf import PdfReader
from PIL import Image, ImageOps, ImageDraw
ROOT=Path(__file__).resolve().parent
qa=ROOT/'qa_final'
pdf=PdfReader(ROOT/'代码大模型文献读后报告_2025-2026.pdf')
pages=[]
for i,p in enumerate(pdf.pages,1):
    text=p.extract_text() or ''
    pages.append({'page':i,'chars':len(text),'start':text[:100],'end':text[-90:]})
(qa/'page_text_check.json').write_text(json.dumps(pages,ensure_ascii=False,indent=2),encoding='utf-8')
images=sorted(qa.glob('page-*.png'))
for n in range(0,len(images),4):
    sheet=Image.new('RGB',(1400,1880),'#D8D8D8')
    draw=ImageDraw.Draw(sheet)
    for j,src in enumerate(images[n:n+4]):
        im=Image.open(src).convert('RGB')
        im.thumbnail((670,890))
        x=15+(j%2)*700;y=25+(j//2)*940
        sheet.paste(im,(x,y+20));draw.text((x,y),src.stem,fill='black')
    sheet.save(qa/f'contact-{n//4+1:02}.png')
print(json.dumps({'pdf_pages':len(pages),'png_pages':len(images),'contacts':(len(images)+3)//4,'short_pages':[p for p in pages if p['chars']<230]},ensure_ascii=True))
