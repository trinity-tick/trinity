# -*- coding: utf-8 -*-
"""probe_con: manually call CON_PROMPT on one session to inspect note quality."""
import json, os, urllib.request

api_key = None
with open(os.path.expanduser('~/.dsh/.credentials.yaml'), 'r', encoding='utf-8-sig') as f:
    for line in f:
        if line.strip().startswith('DEEPSEEK_API_KEY'):
            api_key = line.split(':', 1)[1].strip().strip('"').strip("'")
            break

def llm(system, user, max_tokens=400):
    payload = {'model': 'deepseek-chat', 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'temperature': 0.0, 'max_tokens': max_tokens}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))['choices'][0]['message']['content'].strip()

CON = ('I will give you a chat history between you and a user, as well as a question from the user. '
       'Write reading notes to extract all the relevant user information relevant to answering the answer. '
       'If no relevant information is found, just output "empty". ' + chr(10) + chr(10) +
       'Chat History:' + chr(10) + 'Session Date: {}' + chr(10) + 'Session Content:' + chr(10) + '{}' + chr(10) + chr(10) +
       'Question Date: {}' + chr(10) + 'Question: {}' + chr(10) +
       'Extracted note (information relevant to answering the question):')

# load dataset, take a multi question
d = json.load(open(r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json', encoding='utf-8'))
q = next(x for x in d if x.get('question_type') == 'multi-session')
print('Q:', str(q['question'])[:100])
print('QDATE:', q.get('question_date'))
sess = q['haystack_sessions'][0]
turns = sess if isinstance(sess, list) else sess.get('turns', [])
parts = []
for t_ in turns[:6]:
    role = t_.get('role', 'user') if isinstance(t_, dict) else 'user'
    content_ = t_.get('content', '') if isinstance(t_, dict) else str(t_)
    parts.append('[' + role + '] ' + content_)
text = chr(10).join(parts)
print('SESS TEXT (first 800):', text[:800])
print()
print('=== CON PROMPT OUTPUT (text format) ===')
note1 = llm('', CON.format(q['haystack_dates'][0], text[:6000], q.get('question_date'), q['question']))
print('NOTE:', note1[:300])
print()
print('=== CON PROMPT OUTPUT (official json format) ===')
note2 = llm('', CON.format(q['haystack_dates'][0], json.dumps(turns[:6]), q.get('question_date'), q['question']))
print('NOTE:', note2[:300])
