import { ActionQueue } from "@/components/ActionQueue";

// Client component: the queue mutates (mark done, dismiss, run the sweep) and
// needs TanStack Query's cache invalidation to keep the list honest.
export default function ActionsPage() {
  return <ActionQueue />;
}
