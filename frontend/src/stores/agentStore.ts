import { create } from "zustand";
import { Agents, type Agent, type ToolSpec, Tools } from "../api/client";

interface AgentState {
  agents: Agent[];
  tools: ToolSpec[];
  loading: boolean;
  error?: string;

  loadAgents: () => Promise<void>;
  loadTools: () => Promise<void>;
  createAgent: (data: Partial<Agent>) => Promise<Agent>;
  updateAgent: (id: string, data: Partial<Agent>) => Promise<Agent>;
  deleteAgent: (id: string) => Promise<void>;
}

export const useAgentStore = create<AgentState>((set) => ({
  agents: [],
  tools: [],
  loading: false,
  error: undefined,

  loadAgents: async () => {
    set({ loading: true, error: undefined });
    try {
      const agents = await Agents.list();
      set({ agents, loading: false });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to load agents";
      set({ error: msg, loading: false });
    }
  },

  loadTools: async () => {
    try {
      const tools = await Tools.list();
      set({ tools });
    } catch {
      // tools are optional UI; silently ignore
    }
  },

  createAgent: async (data) => {
    const agent = await Agents.create(data);
    set((s) => ({ agents: [...s.agents, agent] }));
    return agent;
  },

  updateAgent: async (id, data) => {
    const agent = await Agents.update(id, data);
    set((s) => ({ agents: s.agents.map((a) => (a.id === id ? agent : a)) }));
    return agent;
  },

  deleteAgent: async (id) => {
    await Agents.remove(id);
    set((s) => ({ agents: s.agents.filter((a) => a.id !== id) }));
  },
}));