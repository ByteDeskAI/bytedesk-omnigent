export function SearchFilterInput({
  label,
  placeholder,
  value,
  onChange,
}: {
  label: string;
  placeholder: string;
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className="font-medium text-[10px] text-muted-foreground uppercase tracking-wide">
        {label}
      </span>
      <input
        aria-label={label}
        className="w-full rounded border border-border bg-transparent px-2 py-1 font-mono text-xs outline-none placeholder:text-muted-foreground focus:border-ring"
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        type="text"
        value={value}
      />
    </label>
  );
}