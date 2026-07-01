export function MembersLoadingState() {
  return (
    <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
      Loading…
    </div>
  );
}

export function MembersAccessDenied() {
  return (
    <div className="mx-auto w-full max-w-2xl px-6 py-12">
      <h1 className="mb-2 text-2xl font-semibold">Members</h1>
      <p className="text-sm text-muted-foreground">
        You don't have permission to manage members.
      </p>
    </div>
  );
}