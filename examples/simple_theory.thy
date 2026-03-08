theory SimpleTheory
imports Main
begin

text \<open>
  This is a simple Isabelle theory file for testing the MCP server.
\<close>

section \<open>Basic Definitions\<close>

definition my_const :: "nat" where
  "my_const = 42"

definition add_one :: "nat \<Rightarrow> nat" where
  "add_one n = n + 1"

section \<open>Basic Lemmas\<close>

lemma my_const_value: "my_const = 42"
  by (simp add: my_const_def)

lemma add_one_succ: "add_one n = Suc n"
  by (simp add: add_one_def)

lemma add_one_commute: "add_one (add_one n) = n + 2"
  by (simp add: add_one_def)

section \<open>Simple Proofs\<close>

theorem simple_theorem: "my_const + 8 = 50"
proof -
  have "my_const = 42" by (simp add: my_const_def)
  then show ?thesis by simp
qed

lemma arithmetic_fact: "(a::nat) + b = b + a"
  by simp

end
