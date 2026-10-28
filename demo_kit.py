"""Pre-built lesson so the app can be demoed even if Wi-Fi or the Gemini API fails.

Load it with:  flask --app app seed-demo
"""

DEMO_TITLE = "Introduction to Computer Science (demo)"

DEMO_NOTES = (
    "Computer science is the study of computers and computational systems. An algorithm is a step-by-step "
    "set of instructions used to solve a problem. Programming languages allow people to write instructions "
    "that computers can execute. Data structures help organize information efficiently."
)

_C = ["What computer science studies", "Algorithms", "Programming languages", "Data structures"]

DEMO_KIT = {
    "summary": "Computer science studies how problems can be solved with computation: designing algorithms, "
               "expressing them in programming languages, and organizing data so programs work efficiently.",
    "objectives": [
        "Describe what computer science studies",
        "Explain what an algorithm is and why precision matters",
        "Explain the role of programming languages",
        "Describe why data structures affect efficiency",
    ],
    "concepts": [
        {"title": _C[0], "explanation": "Computer science is about computation itself, not just computers as machines: "
         "what can be computed, how, and how efficiently.", "example": "Illustrative example: planning the fastest route between two cities is a computational problem."},
        {"title": _C[1], "explanation": "An algorithm is an ordered, unambiguous list of steps that solves a problem. "
         "Because a computer cannot guess intent, every step must be exact.", "example": "Illustrative example: a recipe is an everyday algorithm; a sorting procedure is a computing one."},
        {"title": _C[2], "explanation": "Programming languages let people write algorithms in a form a computer can execute, "
         "bridging human reasoning and machine instructions.", "example": "Illustrative example: the same algorithm can be written in Python or Java."},
        {"title": _C[3], "explanation": "Data structures organize information so it can be stored, found and changed efficiently. "
         "The right structure can make an algorithm dramatically faster.", "example": "Illustrative example: looking up a name is faster in an index than by reading a whole list."},
    ],
    "key_terms": [
        {"term": "Algorithm", "definition": "A step-by-step set of instructions for solving a problem."},
        {"term": "Programming language", "definition": "A formal language for writing instructions a computer can execute."},
        {"term": "Data structure", "definition": "A way of organizing information so it can be used efficiently."},
    ],
    "recap": [
        "Computer science studies computation, not only machines.",
        "Algorithms are exact step-by-step procedures.",
        "Programming languages make algorithms executable.",
        "Data structures organize information for efficiency.",
    ],
    "questions": [
        {"question": "Why must each step of an algorithm be precise?", "options": [
            "A computer cannot infer what the author meant", "Precise steps make the program shorter",
            "Imprecise steps run faster", "Languages reject long instructions"], "answer": 0, "topic": _C[1],
         "explanation": "Computers follow instructions literally, so ambiguity breaks the procedure."},
        {"question": "Which statement best describes the role of a programming language?", "options": [
            "It stores data on disk", "It expresses algorithms in a form a computer can execute",
            "It decides which problems are worth solving", "It replaces the need for algorithms"], "answer": 1,
         "topic": _C[2], "explanation": "Languages translate human-designed procedures into executable instructions."},
        {"question": "A team's program slows down badly as its list of customers grows. What is the most relevant concept to review?",
         "options": ["Choice of data structure", "Colour scheme of the interface", "Keyboard layout", "Programming language syntax highlighting"],
         "answer": 0, "topic": _C[3], "explanation": "How data is organized strongly affects how fast it can be searched and updated."},
        {"question": "Which is the best example of an everyday algorithm?", "options": [
            "A photograph of a meal", "A recipe with ordered steps", "A list of favourite foods", "A restaurant menu"],
         "answer": 1, "topic": _C[1], "explanation": "An algorithm is an ordered procedure for achieving a result."},
        {"question": "Which view of computer science is most accurate?", "options": [
            "It is mainly about building hardware", "It is the study of computation and how to solve problems with it",
            "It is the same as learning one programming language", "It is only about using software"],
         "answer": 1, "topic": _C[0], "explanation": "The field centres on computation, not on any single tool."},
        {"question": "Two programs solve the same problem, but one organizes its data much better. What is the likely difference?",
         "options": ["The better organized one will be more efficient", "They must run at identical speed",
                     "The poorer one will use less memory in every case", "Only the languages matter"],
         "answer": 0, "topic": _C[3], "explanation": "Data organization is a main driver of efficiency."},
    ],
}
