import { Trash2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { GoalRecord } from "@/lib/goalsApi";

export interface GoalsDeleteDialogProps {
  pendingDelete: GoalRecord | null;
  setPendingDelete: (goal: GoalRecord | null) => void;
  deleteBusy: boolean;
  onConfirmDelete: () => void;
}

export function GoalsDeleteDialog({
  pendingDelete,
  setPendingDelete,
  deleteBusy,
  onConfirmDelete,
}: GoalsDeleteDialogProps) {
  return (
    <Dialog
      open={pendingDelete !== null}
      onOpenChange={(open) => {
        if (!open) setPendingDelete(null);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete goal</DialogTitle>
          <DialogDescription>
            Permanently delete “{pendingDelete?.title}” and its dependencies. This cannot be
            undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button variant="destructive" disabled={deleteBusy} onClick={() => void onConfirmDelete()}>
            <Trash2Icon /> Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}