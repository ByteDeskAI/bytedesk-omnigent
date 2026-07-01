import { useEditorState } from "@tiptap/react";
import {
  Bold,
  Code,
  Heading1,
  Heading2,
  Heading3,
  Italic,
  List,
  ListOrdered,
  Pilcrow,
  Quote,
  Redo2,
  Strikethrough,
  Undo2,
} from "lucide-react";
import type { Editor } from "@tiptap/react";
import { Divider, ToolbarBtn } from "./MarkdownToolbarPrimitives";
import { TableAlignControls, TableBtn } from "./MarkdownTableToolbarControls";

export function MarkdownToolbarFormatSection({ editor }: { editor: Editor | null }) {
  const editorState = useEditorState({
    editor,
    selector: (ctx) => ({
      canUndo: ctx.editor?.can().undo() ?? false,
      canRedo: ctx.editor?.can().redo() ?? false,
      isParagraph:
        (ctx.editor?.isActive("paragraph") &&
          !ctx.editor?.isActive("heading") &&
          !ctx.editor?.isActive("blockquote")) ??
        false,
      isH1: ctx.editor?.isActive("heading", { level: 1 }) ?? false,
      isH2: ctx.editor?.isActive("heading", { level: 2 }) ?? false,
      isH3: ctx.editor?.isActive("heading", { level: 3 }) ?? false,
      isBlockquote: ctx.editor?.isActive("blockquote") ?? false,
      isBold: ctx.editor?.isActive("bold") ?? false,
      isItalic: ctx.editor?.isActive("italic") ?? false,
      isStrike: ctx.editor?.isActive("strike") ?? false,
      isCode: ctx.editor?.isActive("code") ?? false,
    }),
  });

  const {
    canUndo,
    canRedo,
    isParagraph,
    isH1,
    isH2,
    isH3,
    isBlockquote,
    isBold,
    isItalic,
    isStrike,
    isCode,
  } = editorState ?? {};

  return (
    <>
      <ToolbarBtn
        title="Undo (⌘Z)"
        onClick={() => editor?.chain().focus().undo().run()}
        className={!canUndo ? "opacity-30 cursor-default" : ""}
      >
        <Undo2 className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        title="Redo (⌘⇧Z)"
        onClick={() => editor?.chain().focus().redo().run()}
        className={!canRedo ? "opacity-30 cursor-default" : ""}
      >
        <Redo2 className="size-3.5" />
      </ToolbarBtn>
      <Divider />
      <ToolbarBtn
        active={isParagraph}
        title="Normal"
        onClick={() => editor?.chain().focus().setParagraph().run()}
      >
        <Pilcrow className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        active={isH1}
        title="Heading 1"
        onClick={() => editor?.chain().focus().toggleHeading({ level: 1 }).run()}
      >
        <Heading1 className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        active={isH2}
        title="Heading 2"
        onClick={() => editor?.chain().focus().toggleHeading({ level: 2 }).run()}
      >
        <Heading2 className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        active={isH3}
        title="Heading 3"
        onClick={() => editor?.chain().focus().toggleHeading({ level: 3 }).run()}
      >
        <Heading3 className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        active={isBlockquote}
        title="Quote"
        onClick={() => editor?.chain().focus().toggleBlockquote().run()}
      >
        <Quote className="size-3.5" />
      </ToolbarBtn>
      <Divider />
      <ToolbarBtn
        active={isBold}
        title="Bold (⌘B)"
        onClick={() => editor?.chain().focus().toggleBold().run()}
      >
        <Bold className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        active={isItalic}
        title="Italic (⌘I)"
        onClick={() => editor?.chain().focus().toggleItalic().run()}
      >
        <Italic className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        active={isStrike}
        title="Strikethrough"
        onClick={() => editor?.chain().focus().toggleStrike().run()}
      >
        <Strikethrough className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        active={isCode}
        title="Inline code"
        onClick={() => editor?.chain().focus().toggleCode().run()}
      >
        <Code className="size-3.5" />
      </ToolbarBtn>
      <Divider />
      <ToolbarBtn
        title="Bullet list"
        onClick={() => editor?.chain().focus().toggleBulletList().run()}
      >
        <List className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        title="Numbered list"
        onClick={() => editor?.chain().focus().toggleOrderedList().run()}
      >
        <ListOrdered className="size-3.5" />
      </ToolbarBtn>
      <Divider />
      <TableBtn editor={editor} />
      {editor && <TableAlignControls editor={editor} />}
    </>
  );
}