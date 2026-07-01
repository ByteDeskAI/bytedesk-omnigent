import { FileTextIcon, ImageIcon, XIcon } from "lucide-react";
import type { NewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingAttachmentSection({ state }: { state: NewChatLandingState }) {
  const s = state;

  return (
    <>
      <input
        ref={s.fileInputRef}
        type="file"
        multiple
        accept="image/*,application/pdf,text/*,application/json"
        className="hidden"
        data-testid="new-chat-landing-file-input"
        onChange={(e) => {
          if (e.target.files) {
            s.addFiles(Array.from(e.target.files));
            e.target.value = "";
          }
        }}
      />
      {s.files.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-4 pb-2">
          {s.files.map((file, i) => (
            <span
              key={i}
              className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
            >
              {file.type.startsWith("image/") ? (
                <ImageIcon className="size-3 shrink-0" />
              ) : (
                <FileTextIcon className="size-3 shrink-0" />
              )}
              <span className="max-w-[140px] truncate">{file.name || "image.png"}</span>
              <button
                type="button"
                onClick={() => s.removeFile(i)}
                className="ml-0.5 rounded-full hover:text-foreground"
                aria-label={`Remove ${file.name || "image.png"}`}
              >
                <XIcon className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}
    </>
  );
}