"""A single realistic call, driven live, for demoing the agent.

Long messy turns on purpose. A caller does not speak in slot values: they bury the
signal in anecdote, name two things when they mean "everything", report what they have
already tried, and ask two questions at once. This drives exactly that and prints the
full text, which is the only way to judge whether the agent sounds like it understood.

Both defects found the week this was written came from READING a call rather than from
an oracle: a hold phrase inside a question ("hang on, is this going to cost me?") and an
outage advisory that said the same sentence twice. 27 CUJs and 62 offline scenarios were
green through both.

    python demo_call.py [app_id]
"""

import sys, textwrap
sys.path.insert(0,"/Users/fsamuel/Labs/cxas-comcast/.worktrees/conformance/flows-sdk")
import labs_paths; labs_paths.add_sdk_paths(driver=True)
from app.products.slot_studio.studio import state as st
st.apply_settings(mode="hosted", project="ces-deployment-dev", location="us")
from app.products.slot_studio.studio.chat_session import ChatSession
APP=f"projects/ces-deployment-dev/locations/us/apps/{sys.argv[1]}"
ACCT="8069100230359928"
MOCK=("convoy_status=predictive_offline&outage_status=none&network_status=clear"
      "&gateway_status=clear")
TURNS=[
 "Hi, yeah, so this is probably nothing but my son's been complaining all week that "
 "his Xbox keeps kicking him off mid-game, and then last night I noticed Netflix was "
 "buffering constantly, which honestly never used to happen. I already tried "
 "restarting my router, that did nothing. Is it something on your end?",

 "No, no, it's everything really. My wife was on a work call this morning and it "
 "dropped on her twice, and the telly was doing that spinning circle thing. So it's "
 "not just the Xbox. Basically the whole house is rubbish.",

 "Right, OK. Before we do anything though, my neighbour had someone come out last "
 "month and got landed with a bill for it, and I'm not paying a hundred dollars for "
 "somebody to turn it off and on again. So is this going to cost me? And if it's "
 "just a restart, can't you do that from your end without sending anyone?",

 "Yeah, alright then, go on and do the reboot.",

 "How long am I looking at? I've got a call at two that I really can't miss.",
]
s=ChatSession(app_name=APP, initial_variable_state={"accountNumber":ACCT,"account_id":ACCT,"mock_config_string":MOCK})
o=s._sessions.run; s._sessions.run=lambda **kw: o(use_tool_fakes=True, **kw)
def show(t):
    txt=(t.agent_text or "").strip(); h=len(txt)//2
    if h and txt[:h].strip()==txt[h:].strip(): txt=txt[:h].strip()
    print("AGENT :", textwrap.fill(txt,74,subsequent_indent="        ") or "(silence)")
    tc=sorted({c.get("action") for c in (t.tool_calls or [])})
    if tc: print("        [", ", ".join(tc), "]")
show(s.send("<event>session start</event>"))
for u in TURNS:
    if s.is_ended: print("\n(session ended)"); break
    print("\nCALLER:", textwrap.fill(u,74,subsequent_indent="        "))
    show(s.send(u))
