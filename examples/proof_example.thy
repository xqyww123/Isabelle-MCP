theory proof_example
imports Main
begin

text \<open>
  This theory demonstrates various proof techniques.
  Use this to test proof state queries with isabelle_goal.
\<close>

section \<open>Proof by Induction\<close>

lemma sum_first_n: "(\<Sum>i=0..n. i) = (n * (n + 1)) div 2"
proof (induct n)
  case 0
  then show ?case by simp
next
  case (Suc n)
  then show ?case by simp
qed

section \<open>Structured Proofs\<close>

lemma list_append_assoc: "(xs @ ys) @ zs = xs @ (ys @ zs)"
proof (induct xs)
  case Nil
  show ?case by simp
next
  case (Cons x xs)
  have "(x # xs @ ys) @ zs = x # ((xs @ ys) @ zs)"
    by simp
  also have "... = x # (xs @ (ys @ zs))"
    using Cons.hyps by simp
  also have "... = (x # xs) @ (ys @ zs)"
    by simp
  finally show ?case .
qed

section \<open>Apply-Style Proofs\<close>

lemma apply_example:
  assumes "P \<longrightarrow> Q"
  assumes "Q \<longrightarrow> R"
  assumes "P"
  shows "R"
  apply (rule mp)
   apply (rule assms(2))
  apply (rule mp)
   apply (rule assms(1))
  apply (rule assms(3))
  done

section \<open>Cases and Splits\<close>

lemma nat_cases_example:
  fixes n :: nat
  shows "n = 0 \<or> (\<exists>m. n = Suc m)"
proof (cases n)
  case 0
  then show ?thesis by simp
next
  case (Suc m)
  then show ?thesis by simp
qed

section \<open>Proof State Exploration\<close>

text \<open>
  Use isabelle_goal on the following lemma to see how
  the proof state changes at each step.
\<close>

lemma proof_state_example:
  assumes "A \<and> B"
  assumes "B \<longrightarrow> C"
  shows "A \<and> C"
proof -
  from assms(1) have a: "A" by simp  \<comment> \<open>Query goal state here\<close>
  from assms(1) have b: "B" by simp  \<comment> \<open>And here\<close>
  from b assms(2) have c: "C" by simp  \<comment> \<open>And here\<close>
  from a c show ?thesis by simp  \<comment> \<open>Final step\<close>
qed

section \<open>Auto and Sledgehammer\<close>

lemma auto_example:
  "length (xs @ ys) = length xs + length ys"
  by auto  \<comment> \<open>Query state before 'by auto'\<close>

lemma simp_example:
  "rev (rev xs) = xs"
  by simp  \<comment> \<open>Query state before 'by simp'\<close>

end
