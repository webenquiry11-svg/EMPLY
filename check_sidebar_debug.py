import re
path='E:/horilla-1.0/templates/sidebar.html'
text=open(path,'r',encoding='utf-8').read()
pattern=re.compile(r"\{%(.*?)%\}",re.S)
stack=[]
for m in pattern.finditer(text):
    content=m.group(1).strip()
    line=text.count('\n',0,m.start())+1
    parts=content.split()
    tok=parts[0] if parts else ''
    print(line, tok, content.replace('\n',' '))
    if tok in ('if','for','block','comment','with'):
        stack.append((tok,line))
    elif tok in ('elif','else'):
        if not stack or stack[-1][0]!='if':
            print('ERROR',line,tok,'without open if')
    elif tok in ('endif','endfor','endblock','endcomment','endwith'):
        exp={'endif':'if','endfor':'for','endblock':'block','endcomment':'comment','endwith':'with'}[tok]
        if not stack:
            print('ERROR',line,tok,'without opener')
        else:
            top=stack.pop()
            if top[0]!=exp:
                print('MISMATCH',line,'ended',tok,'but top was',top)
print('STACK LEFT:',stack)
