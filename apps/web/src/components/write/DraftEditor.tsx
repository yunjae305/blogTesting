import Highlight from "@tiptap/extension-highlight";
import Image from "@tiptap/extension-image";
import Link from "@tiptap/extension-link";
import { TableKit } from "@tiptap/extension-table";
import { EditorContent, useEditor, type Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { useEffect } from "react";

/**
 * The draft, editable as the article itself rather than as its source.
 *
 * It was a textarea, which meant the images had to travel through it as `[[IMAGE:1]]`
 * markers — text is all a textarea can hold. Here they are the images, and moving one
 * is moving it.
 *
 * The editor's HTML is what is saved. It is not trusted on the way back: the server
 * rebuilds the article from an allowlist, because that HTML is what gets copied into
 * 네이버 and published.
 */
export function DraftEditor({
  html,
  onChange,
}: {
  html: string;
  onChange: (html: string) => void;
}) {
  const editor = useEditor({
    extensions: [
      StarterKit.configure({ heading: { levels: [2, 3] } }),
      // The images are data URLs from the image model, not uploads — there is no file
      // to send anywhere, and allowBase64 is what lets Tiptap keep them.
      Image.configure({ allowBase64: true }),
      Link.configure({ openOnClick: false }),
      TableKit.configure({ table: { resizable: true } }),
      // 원고의 핵심 문장 강조(<mark> 형광펜). 서버 allowlist에도 mark가 있어 저장·발행
      // 경로에서 함께 보존된다 — 확장이 없으면 편집만 해도 형광펜이 사라진다.
      Highlight,
    ],
    content: html,
    onUpdate: ({ editor }) => onChange(editor.getHTML()),
  });

  // Opening 원고 수정 a second time has to show the draft as it now is. Without this
  // the editor keeps the content it was mounted with.
  useEffect(() => {
    if (editor && html !== editor.getHTML()) editor.commands.setContent(html);
    // Only when the incoming draft changes — reacting to the editor's own output
    // would fight the user's typing.
  }, [html]);

  if (!editor) return null;

  return (
    <div className="draft-editor">
      <Toolbar editor={editor} />
      <div className="draft-editor-page">
        <EditorContent editor={editor} className="draft-editor-body" />
      </div>
    </div>
  );
}

function Toolbar({ editor }: { editor: Editor }) {
  const button = (label: string, active: boolean, onClick: () => void, title: string) => (
    <button
      type="button"
      className={`editor-tool ${active ? "active" : ""}`}
      onClick={onClick}
      title={title}
      aria-pressed={active}
    >
      {label}
    </button>
  );

  return (
    <div className="editor-toolbar" role="toolbar" aria-label="원고 서식 도구">
      <div className="editor-tool-group">
        {button("굵게", editor.isActive("bold"), () => editor.chain().focus().toggleBold().run(), "굵게")}
        {button(
          "형광펜",
          editor.isActive("highlight"),
          () => editor.chain().focus().toggleHighlight().run(),
          "형광펜 강조",
        )}
        {button("기울임", editor.isActive("italic"), () => editor.chain().focus().toggleItalic().run(), "기울임")}
      </div>
      <div className="editor-tool-group">
        {button(
          "소제목",
          editor.isActive("heading", { level: 2 }),
          () => editor.chain().focus().toggleHeading({ level: 2 }).run(),
          "소제목",
        )}
        {button("목록", editor.isActive("bulletList"), () => editor.chain().focus().toggleBulletList().run(), "목록")}
        {button(
          "번호 목록",
          editor.isActive("orderedList"),
          () => editor.chain().focus().toggleOrderedList().run(),
          "번호 목록",
        )}
        {button("인용", editor.isActive("blockquote"), () => editor.chain().focus().toggleBlockquote().run(), "인용문")}
      </div>
      <div className="editor-tool-group">
        {button(
          "링크",
          editor.isActive("link"),
          () => {
            if (editor.isActive("link")) {
              editor.chain().focus().unsetLink().run();
              return;
            }
            const url = window.prompt("링크 주소");
            if (url) editor.chain().focus().setLink({ href: url }).run();
          },
          "링크",
        )}
        {button(
          "표",
          editor.isActive("table"),
          () => editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run(),
          "표 삽입",
        )}
      </div>
    </div>
  );
}
