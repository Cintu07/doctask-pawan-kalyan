# where superdocs sells

ten use cases, with named companies that would buy each one.

**a note on honesty first.** i know nobody at any company below. the brief says
that is the normal answer and costs nothing, so i have not dressed it up. these
are companies i picked because their business already has the shape of the
problem, and i have said in each case what makes me think so. i have contacted
none of them and will not.

**the filter i used.** i threw out every idea where the answer is "generate a
document". that is solved and chatgpt does it free. what is left is where
someone already has a document, it is long, it is the source of truth, and
changing it correctly is expensive because a wrong change costs real money. that
is the only place an editing agent with a review gate is worth paying for.

---

## 1. tender and rfp response reuse

**the pain.** a bid team keeps a library of past proposals. every new tender is
80 percent a previous one with the client name, the scope, the dates and the
pricing changed, and the compliance annex swapped. today a junior does it by
hand in word and misses one instance of the old client's name, which is how you
lose a bid.

**why superdocs.** this is targeted editing over a long document with review,
exactly the shape of the product. find and change every commercial term, leave
the technical narrative untouched, prove nothing else moved.

**who buys it**

- **Turner & Townsend** (uk, infrastructure consultancy, ~13,000 staff). they
  bid constantly on public infrastructure work with mandatory response formats.
- **Mott MacDonald** (uk, engineering, employee owned). same shape, heavy
  framework bidding.
- **Capita** and **Serco** (uk outsourcers). public sector bids where format
  compliance is scored and a formatting miss loses points.
- **L&T Technology Services** and **Tata Projects** (india). large tender volume,
  and both already run central bid teams, which is who signs.

**buyer:** head of bid management, or bid ops. it is a cost centre with a
measurable win rate, so it can justify a tool on its own numbers.

---

## 2. clinical trial protocol amendments

**the pain.** a trial protocol is 80 to 200 pages. an amendment changes an
inclusion criterion, and that change has to be propagated into the informed
consent form, the investigator brochure summary, the statistical analysis plan
and every site's local pack. missing one is a regulatory finding.

**why superdocs.** this is the strongest fit on the list, because the value is
entirely in *not* changing anything else. review mode is not a nice extra here,
it is the reason it can be used at all.

**who buys it**

- **Parexel**, **ICON plc**, **Fortrea** (contract research organisations). they
  run amendments as a service line and are measured on turnaround.
- **Syneos Health**, **Novotech** (asia pacific focused cro).
- **Veeva Systems** as a partner rather than a customer: their vault holds these
  documents and they do not do agentic editing inside them.

**buyer:** vp of clinical operations, or head of regulatory writing. budget
exists because protocol amendment cycle time is already a tracked metric.

**caveat i would say out loud in the room:** this industry asks about
certifications on the first call. the honest answer is that certification is a
roadmap conversation, not a current fact, and that this lands first in a
non-regulated adjacent workflow.

---

## 3. fund documentation and side letters

**the pain.** a private fund issues a limited partnership agreement, then
negotiates dozens of side letters that each vary a clause for one investor. by
close, nobody can answer "what does investor 14 actually get" without reading
everything. every new fund vintage restarts it.

**why superdocs.** two jobs: propagate an agreed change through the document set,
and produce a most favoured nation analysis where every entry cites its clause.
that is document editing plus the citation fields the product already keeps
through export.

**who buys it**

- **Intertrust** and **Alter Domus** (fund administrators, thousands of funds).
  they do this work as a service, so the tool is margin.
- **Apex Group**, **Citco**.
- mid market gps directly: **Livingbridge**, **Inflexion** (uk), **Kedaara** and
  **ChrysCapital** (india).

**buyer:** fund administrators buy for margin, gps buy for legal spend. the
administrator is the better first call because they feel the labour cost.

---

## 4. insurance policy wording and endorsements

**the pain.** an insurer maintains base wordings and layers endorsements on them
per client. a regulatory change or a court decision means one clause must be
updated across hundreds of live wordings, each with its own local variations.

**why superdocs.** targeted clause replacement across a document set, with a
human approving each one, and proof that the client specific variations survived
untouched.

**who buys it**

- **Beazley**, **Hiscox**, **Brit** (lloyd's syndicates, specialty lines,
  wording heavy).
- **Markel International**.
- **ICICI Lombard** and **HDFC ERGO** (india, high policy volume).
- broker side: **Howden**, **Lockton**.

**buyer:** head of underwriting operations, or the wordings committee.

---

## 5. construction contract variations

**the pain.** a jct or fidic contract plus a stream of variation orders, site
instructions and payment notices. by month eight the contract as agreed and the
contract as operated have drifted, and the difference is the dispute.

**why superdocs.** keep one living contract position document that updates as
variations land, with every clause citing the instruction that changed it. this
is close to what i built for task one, which is partly why i picked the domain.

**who buys it**

- **Balfour Beatty**, **Kier Group**, **Galliford Try** (uk contractors).
- **Laing O'Rourke**.
- quantity surveying practices: **Gleeds**, **Rider Levett Bucknall**.
- **Larsen & Toubro** (india) at a different scale entirely.

**buyer:** commercial director or head of quantity surveying. contractors are
slow buyers, so the qs practice is the faster first sale.

---

## 6. sop and quality manual maintenance under audit

**the pain.** a manufacturer holds hundreds of standard operating procedures
under iso 9001 or iso 13485. a process change means every affected sop, work
instruction and training record must be updated and version controlled, and an
auditor will ask to see the change history.

**why superdocs.** version history with revert and a review gate already exist in
the product. the missing piece is propagating one change across a document set,
which is the agentic part.

**who buys it**

- **Renishaw**, **Spirax-Sarco** (uk precision manufacturing, iso heavy).
- **Smith+Nephew** (medical devices, 13485).
- **Sun Pharma** and **Cipla** (india, pharma manufacturing, heavy sop load).

**buyer:** head of quality, or qa document control. this role often already owns
a document management budget, so it is a swap rather than a new line.

---

## 7. grant reporting for research and ngos

**the pain.** a funder wants a report in their template, quarterly. the content
is 70 percent the same each quarter with figures updated and one narrative
section rewritten. multiply by fifteen funders, each with a different template.

**why superdocs.** templates plus targeted edits plus export. it is the same
shape as my task two build, which is what makes me confident the mechanics work.

**who buys it**

- **Wellcome Trust** grantees, and university research offices: **Imperial
  College London**, **University of Manchester**.
- **Oxfam GB**, **Save the Children UK** (many funders, many templates).
- **Wadhwani Foundation** and **Azim Premji Foundation** (india).

**buyer:** research office director, or head of programmes. honest note: budgets
here are small. it is a credibility and volume play, not a revenue play.

---

## 8. bank credit memos and annual reviews

**the pain.** a relationship manager writes a credit memo for a borrower, then
rewrites most of it every year at review. the financials change, the covenants
mostly do not, and the risk narrative needs to reflect what actually moved.

**why superdocs.** update the parts the numbers moved, leave the rest, and cite
the source for every figure. this is my task two build pointed at a different
document.

**who buys it**

- **Shawbrook Bank**, **OakNorth** (uk mid market lenders, already technical).
  oaknorth in particular sells credit analytics, so they buy or partner.
- **Investec**.
- **HDFC Bank** and **Kotak Mahindra** (india, large sme lending books).
- debt funds: **Beach Point**, **Pemberton**.

**buyer:** head of credit risk, or coo of lending. strong fit because the pain is
annual, predictable and staffed.

---

## 9. technical documentation kept in step with a changelog

**the pain.** a product ships weekly. the docs, the api reference, the customer
facing release notes and the onboarding pdf all drift. the docs team learns
about a breaking change from a customer.

**why superdocs.** this is the coding tool integration the task document says
they keep wishing existed. a ci job takes a merged pull request and proposes the
doc edits, a human approves them in review mode.

**who buys it**

- **Postman**, **Zoho**, **Freshworks** (india, large doc surfaces).
- **Snyk**, **Paddle**, **GoCardless** (uk).
- **Redocly** and **Mintlify** as partners rather than buyers: they own the
  publishing layer and do not do agentic editing.

**buyer:** head of developer experience, or docs lead. small budgets, but the
shortest path to a public case study and the audience most likely to try it
unprompted.

---

## 10. long form book and manual continuity

**the pain.** the task document names this one directly: a thousand page book
that loses its context and its plotline. the real commercial version is a
technical manual or a textbook in its ninth edition, where a change in chapter
three contradicts chapter nineteen and nobody notices until a reader does.

**why superdocs.** continuity checking across a very long document, with edits
proposed in place and approved by an editor. genuinely hard, and nobody serves
it well.

**who buys it**

- **Pearson**, **Cambridge University Press**, **Oxford University Press**
  (textbook editions, exactly this problem).
- **Wiley**, for professional and technical titles.
- **Haynes** (manuals) and standards bodies: **BSI Group**.
- **S. Chand** and **Navneet Education** (india, school textbooks, annual
  revisions to a syllabus).

**buyer:** commissioning editor or head of production. long sales cycle, but it
is the use case with the least competition and the best story.

---

## if i had to pick

**start with fund administrators and croa.** both do this work as a service, so
the tool is margin rather than headcount, which is a much easier conversation
than "this replaces a person". both have a small number of large buyers, so ten
conversations covers the market.

**the fastest proof** is developer documentation, because they will try it
without a procurement cycle and they write publicly about tools they like.

**the one i would build first** is construction variations, because i built the
engine for it in task one and could demo it against a real document set inside a
week.
