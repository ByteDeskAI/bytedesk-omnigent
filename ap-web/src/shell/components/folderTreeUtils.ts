import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

export interface FileNode {
  type: "file";
  name: string;
  file: WorkspaceFile;
}

export interface DirNode {
  type: "dir";
  name: string;
  path: string;
  children: TreeNode[];
  lazy?: boolean;
}

export type TreeNode = FileNode | DirNode;

export function buildTree(files: WorkspaceFile[]): TreeNode[] {
  const root: DirNode = { type: "dir", name: "", path: "", children: [] };

  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;

    if (file.type === "directory") {
      for (let i = 0; i < parts.length - 1; i++) {
        const part = parts[i];
        let dir = node.children.find((c): c is DirNode => c.type === "dir" && c.name === part);
        if (!dir) {
          dir = { type: "dir", name: part, path: parts.slice(0, i + 1).join("/"), children: [] };
          node.children.push(dir);
        }
        node = dir;
      }
      const lastName = parts[parts.length - 1];
      if (!node.children.find((c) => c.type === "dir" && c.name === lastName)) {
        node.children.push({
          type: "dir",
          name: lastName,
          path: file.path,
          children: [],
          lazy: true,
        });
      }
      continue;
    }

    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      let dir = node.children.find((c): c is DirNode => c.type === "dir" && c.name === part);
      if (!dir) {
        dir = { type: "dir", name: part, path: parts.slice(0, i + 1).join("/"), children: [] };
        node.children.push(dir);
      }
      node = dir;
    }
    node.children.push({ type: "file", name: parts[parts.length - 1], file });
  }

  function sort(node: DirNode) {
    node.children.sort((a, b) => {
      if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    for (const child of node.children) {
      if (child.type === "dir") sort(child);
    }
  }
  sort(root);

  return root.children;
}

export const expandedPathsCache = new Map<string, Set<string>>();

export function defaultExpandedPaths(files: WorkspaceFile[]): Set<string> {
  const tree = buildTree(files);
  const paths = new Set<string>();
  function collect(nodes: TreeNode[]) {
    for (const node of nodes) {
      if (node.type === "dir" && !node.lazy) {
        paths.add(node.path);
        collect(node.children);
      }
    }
  }
  collect(tree);
  return paths;
}