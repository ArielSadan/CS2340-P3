Developer knowledge-base management: see DEVELOPER_SETUP.md for setup and tests.
Page: /developer/knowledge/ (staff login required). /chat/developer/ redirects there.

Joud on 11/10/26 at 3:05PM
My story (#2, asking questions in the chat) is in the repo. It's a new Django app at medsmanagement/chat/ and the page is at /chat/. TLDR:

Everyone
- Run `pip install -r requirements.txt` and then `python manage.py migrate`. so we are on the same page
- Set the GEMINI_API_KEY environment variable locally. Don't commit it. Without it, the chat page still loads but every answer shows an error message.
- db.sqlite3 is now in .gitignore because our vectors live in it. I think if you commit it we'll get merge conflicts so be careful. 
- The chat lives at /chat/, so the root URL / is free for the home page. The home app isn't in INSTALLED_APPS yet; whoever owns it needs to add it there.
- Run the tests with `python manage.py test chat`.
- keep in mind there is no data yet so right now everything is "off-topic" for the chatbot.

#4 Show sources
- The API already returns a `sources` list (the source name of each chunk used), and the chat shows a plain "Sources:" line under each answer.
- To change how sources look, edit addMessage() in chat/static/chat/chat.js.
- If you need more than the name (a link, a page number) you can add a field to the DocumentChunk model and include it in the response.

#5 / #6 / #7 Dataset, ingestion, cleaning
- Vectors are stored in SQLite in the model chat.models.DocumentChunk, with the fields source, text and embedding.
- Save chunks through `chat.services.retrieval.add_chunks([(source, text), ...])`. It embeds them with gemini-embedding-001 and stores them in the format retrieval expects. If you write rows yourself or use a different embedding model, every answer will fail. we can technically use a different model they just have to match, decision is up to you, you can update whatever if you already made your own chunk model
- `source` is what users see as the citation so make it readable like an article title not a file path.
- Clean and chunk the text before calling add_chunks.

#8 Dynamic prompts
- Your hook is build_prompt(question, chunks) in chat/services/rag.py. Replace it with your intent-based template selection. The base system instruction is SYSTEM_INSTRUCTION in the same file.
- Keep the rule "reply exactly NO_ANSWER if the context doesn't contain the answer" in every prompt. Off-topic detection depends on it.

#9 Accuracy testing
- chat/tests.py already has 11 tests with Gemini mocked: normal answers, off-topic, errors and retrieval so just add yours there
- To test end to end, call `rag.answer_question(question)` directly. It returns answer, status, sources and latency_ms.
- RAG_MIN_SIMILARITY = 0.55 in settings.py is a random number i guessed. Your known questions would be great for tuning it.

#10 Performance monitoring
- Every answer comes back with latency_ms and a status: "ok", "off_topic" or "error". Errors are also logged under the logger name chat.services.rag.
- The easiest place to record them is the chat_api view in chat/views.py, right after the answer_question() call.

#11 Clear / reload knowledge base
- Clearing is `DocumentChunk.objects.all().delete()`. Reloading means re-running ingestion through add_chunks.
- The chat picks up the change on the next question automatically, with no server restart needed (hopefully)

#12 Usage logs
- Every request to POST /chat/api/chat/ includes a session_id (random for each page load), and the response returns session_id, status and latency_ms.
- Log one row per question from the chat_api view in chat/views.py.
- #10 and #12 need the same data, so I'd suggest the two of you share one QueryLog model instead of building two.
