Xfinity Internet Repair - review workbench
==========================================

WHAT IS IN THIS ZIP

  Xfinity Internet Repair workbench.html    The whole review surface, one file.
  agent/                                    The agent's source, 262 files.
  flows-sdk-package/                        The Flows SDK itself, 387 files,
                                            at commit 0a6d2e06, the same commit
                                            the toolkit pages came from.

HOW TO OPEN

Double-click the html. It opens in your browser. There is nothing to install,
no server to start, and it works with no network connection. The whole site is
inside that one file, so you can move it or forward it as it is.

Tested in Chrome, Edge and Firefox.


WHAT IS IN THE WORKBENCH

Introduction        What you are reviewing and how to read it. Start here.

Testing in demo     How to try any of these journeys yourself: the account
mode                numbers, the exact words to type, and how long to wait
                    between them. Read it before you demonstrate anything.

User journeys       Fifteen designs, one per path a caller can take. Each has
                    up to four tabs:
                      Flow           - play the call a turn at a time and watch
                                       the graph light up
                      Conversations  - the same calls written out, alongside
                                       real recordings of the older agent
                      Try it         - the account number and the words to type
                                       to see this one happen
                      Detail         - the trigger, what "correct" means, and
                                       what must not happen

System behaviour    Two pages of rules that apply to every call rather than to
                    one path: voice and safety rules, and the order the agent
                    resolves findings in.

Captured            Sixteen recordings of the agent as it ran BEFORE this
conversations       rebuild. They are the evidence behind the designs. They are
                    not what the agent does today.

Learn the agent     A course, for engineers. Twenty two lessons in four parts,
                    from never having heard of the toolkit to being able to
                    change what a caller is told and know which check catches a
                    mistake. You run something in lesson two.

Architecture        Twelve lookup tables generated from the built agent: the
reference           ladder, the slots, every decision, the tools, the file map,
                    the checks and the commands.

Toolkit reference   The complete Flows SDK documentation, 164 pages including
                    70 runnable examples, current as of the date below.


ABOUT agent/

The source the workbench describes. Build output is deliberately excluded, so
build it yourself rather than reading a stale copy:

    cd agent && make -C .. agent

The lesson "Build it and grade it" walks through that and what to run next.


ABOUT flows-sdk-package/

The toolkit the agent is built with, as source: the library under src/, all 70
runnable examples under examples/, its own tests, and a pyproject.toml so it can
be installed.

It is taken from the exact commit the Toolkit reference pages were generated
from, so the code and the pages agree. Every page under "Runnable examples" in
the workbench is generated from a file in examples/, and each of those pages
prints the command that runs it.


TWO THINGS WORTH KNOWING

The agent speaks first. On a phone call the platform opens the session, so the
agent owns the opening turn and asks what is going on before the caller has said
anything. Every conversation here starts that way, because every real call does.

You can say what you think. Add a comment anywhere on a conversation and use
Export to hand yourself a small .json file with your notes in it; send that back
and each note comes with the exact turn it was about. You can also correct the
wording directly: on a Conversations tab choose Edit, change it, then Save, which
hands you a .json file the same way. Comments say why, edits say what instead,
and both are useful.

Your notes live in this browser tab. Export before you close it.
