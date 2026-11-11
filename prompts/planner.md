You are planning a project for Jason, who runs this tool. He is not a programmer: write
everything in plain English. **Do not write any app code, do not build anything and do not
install anything.** Your only job here is the plan.

Project: {{NAME}} ({{KIND}})
Project folder: {{FOLDER}} - write your two plan files here, nothing else.

Jason's outline:
"""
{{OUTLINE}}
"""

Do exactly this:

1. Look at the folder first (`ls`). If it already has code, read enough of it to plan
   realistically; if it is empty, plan from the outline alone.
2. Write `ROADMAP.md`: the milestones in plain English. One heading per milestone, two or
   three sentences under it saying what you will be able to do when that milestone is done.
   Nothing else in that file.
3. Write `tasks.json`: a JSON array of tasks, in the order they should be done. Every task:

       {
         "id": "t1",                     short, unique, only letters digits . _ -
         "milestone": "M1 - it runs",    the milestone from ROADMAP.md it belongs to
         "title": "One line, what you get",
         "prompt": "What the agent should do, in plain English.",
         "acceptance": ["A check a person can test."],
         "checks": ["the command that proves it"],   optional
         "depends_on": ["ids that must be done first"],   optional
         "size": "S",                    S = about 20 minutes, M = about 45
         "screen": "What the screen looks like"        only for tasks with a screen
       }

   Nothing outside those fields - the plan is refused if a task uses another field name.

Planner rules - these are checked, so follow them:

* Every task must be finishable in about 45 minutes. If it is bigger, split it.
* Task 1 sets up the project skeleton, the test setup and `AGENT_CHECKS.json` (the commands
  the board will run after every later task), so from task 2 on there is always something to
  check against.
* Every task has testable acceptance criteria: things you can run or look at and answer
  yes/no to ("`python3 -m pytest` passes", "the app starts and shows a list of notes"), never
  "works well" or "is clean".
* A task with a screen says what that screen looks like in `screen`.
* `depends_on` only for what really has to come first, and never on itself or in a circle.
* Aim for 3-8 milestones and 6-25 tasks.

{{ERRORS}}Write both files and stop. Do not touch anything else in the folder.
