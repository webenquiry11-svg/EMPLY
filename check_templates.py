import re, os
root=r'E:/horilla-1.0/templates'
pattern=re.compile(r"\{%(.*?)%\}",re.S)
openers={'if':'endif','for':'endfor','block':'endblock','comment':'endcomment','with':'endwith'}
issues=[]
for dirpath, dirs, files in os.walk(root):
    for f in files:
        if not f.endswith('.html'):
            continue
        path=os.path.join(dirpath,f)
        try:
            text=open(path,encoding='utf-8').read()
        except Exception as e:
            issues.append((path,'ERROR',str(e)))
            continue
        stack=[]
        for m in pattern.finditer(text):
            content=m.group(1).strip()
            line=text.count('\n',0,m.start())+1
            parts=content.split()
            tok=parts[0] if parts else ''
            if tok in openers:
                stack.append((tok,line))
            elif tok in ('elif','else'):
                if not stack or stack[-1][0]!='if':
                    issues.append((path,line,f"{tok} without open if"))
            elif tok in ('endif','endfor','endblock','endcomment','endwith'):
                exp=None
                for k,v in openers.items():
                    if v==tok:
                        exp=k;break
                if not stack:
                    issues.append((path,line,f"{tok} without opener"))
                else:
                    top=stack.pop()
                    if top[0]!=exp:
                        issues.append((path,line,f"Mismatched end {tok}, expected end for {top[0]} opened at line {top[1]}"))
        if stack:
            for s in stack:
                issues.append((path,'EOF',f"Unclosed {s[0]} opened at line {s[1]}"))

if not issues:
    print('No issues found across templates')
else:
    for it in issues:
        print(it)
